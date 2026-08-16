"""Taigi TTS Pipecat Service.

Wraps the shared orchestration in `services/taigi_tts.py` (also used by the
REST `/api/tts` endpoint, `api/tts.py`) into a Pipecat TTSService.
Pipeline:
  0. Cache      — 先查 `_decoded_cache`（(model, voice, 正規化後文字) → 已解碼音訊），
                  命中就跳過 1-4 步驟；命中率取決於文字重複度（歡迎詞、固定回覆等）
  1. HanloFlow  — Mandarin → 漢羅混合文字
  2. Taibun     — 漢羅 → Tailo 台羅拼音
  3. Split      — Tailo 在 , 和 . 切段
  4. TTS HTTP   — 各段並發送至 /v1/audio/speech，回傳 WAV
  5. PCM yield  — 從 WAV 提取 PCM，先 yield SubtitleFrame（含總時長），再轉成
                  OutputAudioRawFrame 串流給前端，讓字幕與音訊播放同步；成功結果
                  寫回 `_decoded_cache`
"""

import asyncio
import io
import logging
import time
import wave
from collections import OrderedDict
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Any

import httpx
from pipecat.frames.frames import DataFrame, Frame, TTSAudioRawFrame
from pipecat.services.settings import TTSSettings
from pipecat.services.tts_service import TextAggregationMode, TTSService

from pipeline.tts_normalizer import normalize_for_tts
from services.taigi_tts import (
    TTSConfigError,
    TTSEmptyResultError,
    TTSSegmentLimitError,
    load_tts_config,
    make_silence_pcm,
    prepare_tailo,
    synthesize_segments,
)
from telemetry import get_telemetry

_log = logging.getLogger(__name__)


@dataclass
class SubtitleFrame(DataFrame):
    """Carries the full display text for one run_tts() call plus its total
    audio duration, yielded before any audio frames so a downstream processor
    can start a playback-synced reveal animation at the moment audio starts
    queuing (see SubtitleSyncProcessor in voice/pipeline.py).

    Deliberately NOT a TTSTextFrame subclass: pipecat's WordCompletionTracker
    still emits a default end-of-segment TTSTextFrame (push_text_frames stays
    True — see tts_taigi.py module docstring/CLAUDE.md), and this frame must
    stay distinguishable from that one. No `pts` is set (inherited as None),
    so BaseOutputTransport queues it inline with audio at real-time speed.
    """

    text: str
    duration_ms: int


def _extract_pcm(wav_bytes: bytes) -> tuple[bytes, int, int, int]:
    """Extract raw PCM bytes, sample rate, channel count, and sample width (bytes/sample) from a WAV file."""
    with wave.open(io.BytesIO(wav_bytes)) as wf:
        return wf.readframes(wf.getnframes()), wf.getframerate(), wf.getnchannels(), wf.getsampwidth()


# Decoded-segment tuple: (pcm_bytes, sample_rate, num_channels, sample_width, silence_ms)
_DecodedSegment = tuple[bytes, int, int, int, int]

# Bounded-by-bytes LRU cache of fully-decoded audio for repeated fixed text
# (welcome greeting, fallback error replies, etc.) so a repeat hit skips both
# the Hanlo/Taibun conversion and the upstream TTS HTTP round trip entirely —
# the biggest win for time-to-first-audio on those lines. Keyed on
# (config.model, config.voice, post-normalize_for_tts text) — folding in model/
# voice keeps a runtime TTS_MODEL/TTS_VOICE change from serving stale audio
# synthesized under the old config; module-level (not per-TaigiTTSService
# instance) so the benefit is shared across sessions in this process.
#
# Budget is in *bytes of raw PCM*, not entry count: a count cap treats a
# multi-second, multi-segment reply the same as a two-word one, and this
# process runs for weeks without restarting, so a handful of long replies
# under a count cap can quietly hold tens to hundreds of MB. A single decoded
# reply at 24kHz/16-bit/mono runs roughly 48 KB/sec of speech, so a few
# seconds of audio is a few hundred KB; 12 MB comfortably holds several dozen
# distinct fixed replies (welcome greeting, ASR-failure sentence, error
# sentences, ...) while keeping this cache's worst case in the low tens of MB
# regardless of how many distinct dynamic (non-repeating) replies pass through.
_DECODED_CACHE_MAX_BYTES = 12 * 1024 * 1024

# entry value: (decoded segments, total display duration ms, entry size in PCM bytes)
_CacheEntry = tuple[list[_DecodedSegment], int, int]
_decoded_cache: OrderedDict[str, _CacheEntry] = OrderedDict()
_decoded_cache_bytes = 0


def _entry_size_bytes(decoded: list[_DecodedSegment]) -> int:
    return sum(len(pcm_bytes) for pcm_bytes, *_rest in decoded)


def _decoded_cache_get(key: str) -> tuple[list[_DecodedSegment], int] | None:
    entry = _decoded_cache.get(key)
    if entry is None:
        return None
    _decoded_cache.move_to_end(key)
    decoded, total_duration_ms, _size = entry
    return decoded, total_duration_ms


def _decoded_cache_put(key: str, value: tuple[list[_DecodedSegment], int]) -> None:
    global _decoded_cache_bytes
    decoded, total_duration_ms = value
    size = _entry_size_bytes(decoded)
    if size > _DECODED_CACHE_MAX_BYTES:
        # A single reply alone would blow the whole budget (e.g. a long
        # dynamic answer with a station list) — skip caching it rather than
        # evicting every other entry to make room for something that, being
        # dynamic, is unlikely to be asked for again anyway.
        return

    existing = _decoded_cache.get(key)
    if existing is not None:
        _decoded_cache_bytes -= existing[2]
    _decoded_cache[key] = (decoded, total_duration_ms, size)
    _decoded_cache.move_to_end(key)
    _decoded_cache_bytes += size

    while _decoded_cache_bytes > _DECODED_CACHE_MAX_BYTES:
        _, evicted = _decoded_cache.popitem(last=False)
        _decoded_cache_bytes -= evicted[2]


def _decoded_cache_total_bytes() -> int:
    """Current running total of PCM bytes held by the cache — a function, not
    a re-exported module int, so callers (tests included) always see the live
    value instead of a snapshot frozen at import time."""
    return _decoded_cache_bytes


def _decoded_cache_clear() -> None:
    """Reset both the entry dict and the running byte total together — tests
    must not call `_decoded_cache.clear()` directly, which would leave
    `_decoded_cache_bytes` stale and desync the eviction threshold."""
    global _decoded_cache_bytes
    _decoded_cache.clear()
    _decoded_cache_bytes = 0


class TaigiTTSService(TTSService):
    """Custom TTS Service for Taigi Bus Agent."""

    def __init__(self, turn_timer: Any | None = None, **kwargs):
        # TOKEN mode: upstream chunks are already speakable pieces (the agent's
        # StreamNormalizer emits at sentence/pause boundaries). The default
        # SENTENCE aggregator would re-buffer them until a sentence-final mark,
        # holding back long enumerations (e.g. station lists joined by 、) and
        # delaying first audio.
        super().__init__(
            text_aggregation_mode=TextAggregationMode.TOKEN,
            settings=TTSSettings(model=None, voice=None, language=None),
            **kwargs,
        )
        self._turn_timer = turn_timer

    def _discard_tts_context(self, context_id: str) -> None:
        """Drop the base class's per-context bookkeeping entry for `context_id`.

        Pipecat's `TTSService` registers one `TTSContext` per synthesis context in
        `_tts_contexts` (tts_service.py:1080) and only ever deletes it when a
        `TTSStoppedFrame` carrying that context_id passes through `push_frame`
        (tts_service.py:833). Every site that emits a `TTSStoppedFrame` is gated on
        `push_stop_frames`, which we leave at its default False, so that deletion
        never runs for us — and `_handle_interruption` (tts_service.py:912-947)
        does not touch the dict either. Result: one stranded entry per LLM turn
        (context ids are reused within a turn), forever, in a kiosk process that
        stays up for weeks. Each entry is small (a uuid key plus two bools), so
        this is unbounded growth rather than a fast leak — but barge-in is
        constant at a bus stop and nothing else ever reclaims these.

        Called only from the two per-context lifecycle hooks below, each of which
        pipecat invokes exactly once with the id of the context that just ended,
        so this can never drop a context that is still in flight (synthesis for
        turn N+1 can register its entry while turn N is still playing back).
        Audio output is unaffected: nothing reads `_tts_contexts` after these
        points — the `TTSStartedFrame` stamping at tts_service.py:1525 only runs
        under `push_start_frame=True` (we keep the default False), and
        `_push_tts_frames` re-registers the entry before appending anything to a
        recreated context.
        """
        self._tts_contexts.pop(context_id, None)

    async def on_audio_context_completed(self, context_id: str) -> None:
        """Clean up after an audio context that played to the end."""
        await super().on_audio_context_completed(context_id=context_id)
        self._discard_tts_context(context_id)

    async def on_audio_context_interrupted(self, context_id: str) -> None:
        """Clean up after an audio context cut short by barge-in."""
        await super().on_audio_context_interrupted(context_id=context_id)
        self._discard_tts_context(context_id)

    async def run_tts(  # pyright: ignore[reportIncompatibleMethodOverride]
        self,
        text: str,
        context_id: str,
    ) -> AsyncGenerator[Frame | None, None]:
        """Synthesize text and yield AudioRawFrame.

        Whole call is timed via record_pipeline_stage(stage="voice.tts"); outcome
        defaults to "error" and only flips to "ok" once at least one audio frame
        was produced, so config/text-process/all-segments-failed paths all count
        as failures without needing a separate flag per early-return site.
        """
        if not text.strip():
            return

        t0 = time.perf_counter()
        outcome = "error"
        try:
            try:
                config = load_tts_config()
            except TTSConfigError as exc:
                _log.error("TTS config error: %s", exc)
                return

            tts_text = normalize_for_tts(text)
            # Include (model, voice) in the key — otherwise switching TTS_VOICE/
            # TTS_MODEL at runtime (or across sessions with different config) would
            # keep serving audio synthesized under the old voice for text that was
            # cached before the change.
            cache_key = f"{config.model}\x1f{config.voice}\x1f{tts_text}"
            cached = _decoded_cache_get(cache_key)
            if cached is not None:
                decoded, total_duration_ms = cached
            else:
                # Step 1-3: Mandarin → 漢羅 → Tailo → split at punctuation
                try:
                    prep = await prepare_tailo(tts_text)
                except TTSEmptyResultError:
                    _log.warning("Empty Tailo result for input: %s", text)
                    return
                except TTSSegmentLimitError as exc:
                    _log.error("TTS segment limit exceeded: %s", exc)
                    return
                except Exception as exc:
                    _log.error("TTS text process error: %s", exc)
                    return
                segments = prep.segments
                if not segments:
                    return

                # Fixed-size worker pool prevents punctuation-heavy text from
                # creating one asyncio task and one upstream request per segment.
                try:
                    responses = await synthesize_segments(config, segments)
                except TimeoutError:
                    outcome = "timeout"
                    _log.error("TTS synthesis exceeded the total deadline")
                    return

                # Decode all segments first so we know the total duration before
                # yielding anything — the subtitle must precede the first audio
                # frame (see SubtitleFrame docstring / module docstring above).
                decoded = []
                for (seg_text, silence_ms), resp in zip(segments, responses, strict=True):
                    if isinstance(resp, Exception):
                        _log.error("TTS HTTP request failed: %s", resp)
                        continue

                    if resp.status_code != 200:
                        _log.error("TTS upstream error %d: %s", resp.status_code, resp.text)
                        continue

                    try:
                        pcm_bytes, sample_rate, num_channels, sample_width = _extract_pcm(resp.content)
                    except Exception as exc:
                        _log.error("WAV extraction failed: %s", exc)
                        continue

                    decoded.append((pcm_bytes, sample_rate, num_channels, sample_width, silence_ms))

                if not decoded:
                    if any(isinstance(r, httpx.TimeoutException) for r in responses):
                        outcome = "timeout"
                    return

                total_duration_ms = round(
                    sum(
                        (len(pcm_bytes) / (sample_rate * num_channels * sample_width)) * 1000 + silence_ms
                        for pcm_bytes, sample_rate, num_channels, sample_width, silence_ms in decoded
                    )
                )
                # Only fixed/repeatable text benefits, but there's no cheap way to
                # tell "fixed" from "dynamic" apart here — cache every successful
                # synthesis and let the bounded LRU evict dynamic one-offs on its own.
                _decoded_cache_put(cache_key, (decoded, total_duration_ms))

            yield SubtitleFrame(text=text, duration_ms=total_duration_ms)

            first_frame = True
            for pcm_bytes, sample_rate, num_channels, sample_width, silence_ms in decoded:
                # 20ms chunks
                chunk_size = int(sample_rate * 0.02) * num_channels * sample_width

                for i in range(0, len(pcm_bytes), chunk_size):
                    if first_frame and self._turn_timer:
                        self._turn_timer.mark_first_audio()
                        first_frame = False
                    yield TTSAudioRawFrame(
                        audio=pcm_bytes[i : i + chunk_size],
                        sample_rate=sample_rate,
                        num_channels=num_channels,
                    )

                if silence_ms > 0:
                    silence = make_silence_pcm(silence_ms, sample_rate, num_channels, sample_width)
                    for i in range(0, len(silence), chunk_size):
                        if first_frame and self._turn_timer:
                            self._turn_timer.mark_first_audio()
                            first_frame = False
                        yield TTSAudioRawFrame(
                            audio=silence[i : i + chunk_size],
                            sample_rate=sample_rate,
                            num_channels=num_channels,
                        )

            outcome = "ok"
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        finally:
            get_telemetry().record_pipeline_stage(time.perf_counter() - t0, stage="voice.tts", outcome=outcome)
