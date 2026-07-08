"""Taigi TTS Pipecat Service.

Wraps the existing HTTP proxy logic from `api/tts.py` into a Pipecat TTSService.
Pipeline:
  1. HanloFlow  — Mandarin → 漢羅混合文字
  2. Taibun     — 漢羅 → Tailo 台羅拼音
  3. Split      — Tailo 在 , 和 . 切段
  4. TTS HTTP   — 各段並發送至 /v1/audio/speech，回傳 WAV
  5. PCM yield  — 從 WAV 提取 PCM，轉成 OutputAudioRawFrame 串流給前端
"""

import asyncio
import io
import logging
import time
import wave
from collections.abc import AsyncGenerator
from typing import Any

import httpx
from pipecat.frames.frames import Frame, TTSAudioRawFrame
from pipecat.services.settings import TTSSettings
from pipecat.services.tts_service import TTSService

from api.tts import _TTS_TIMEOUT_SECONDS, _split_tailo, _tts_config
from pipeline.text_processor import process_async as text_process_async
from pipeline.tts_normalizer import normalize_for_tts
from providers.http import get_http_client
from telemetry import get_telemetry

_log = logging.getLogger(__name__)


def _extract_pcm(wav_bytes: bytes) -> tuple[bytes, int, int]:
    """Extract raw PCM bytes, sample rate, and channels from a WAV file."""
    with wave.open(io.BytesIO(wav_bytes)) as wf:
        return wf.readframes(wf.getnframes()), wf.getframerate(), wf.getnchannels()


def _make_silence_pcm(duration_ms: int, sample_rate: int, num_channels: int, sampwidth: int = 2) -> bytes:
    """Generate raw PCM silence."""
    n_frames = int(sample_rate * duration_ms / 1000)
    return b"\x00" * (n_frames * num_channels * sampwidth)


class TaigiTTSService(TTSService):
    """Custom TTS Service for Taigi Bus Agent."""

    def __init__(self, turn_timer: Any | None = None, **kwargs):
        super().__init__(settings=TTSSettings(model=None, voice=None, language=None), **kwargs)
        self._turn_timer = turn_timer

    async def run_tts(self, text: str, context_id: str) -> AsyncGenerator[Frame | None, None]:
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
                base_url, model, voice, api_key = _tts_config()
            except Exception as exc:
                _log.error("TTS config error: %s", exc)
                return

            # Step 1 + 2: Mandarin → 漢羅 → Tailo
            try:
                tts_text = normalize_for_tts(text)
                result = await text_process_async(tts_text)
                if not result.tailo:
                    _log.warning("Empty Tailo result for input: %s", text)
                    return
            except Exception as exc:
                _log.error("TTS text process error: %s", exc)
                return

            # Step 3: Split Tailo at punctuation
            segments = _split_tailo(result.tailo)
            if not segments:
                return

            # Step 4: Concurrent TTS requests
            req_headers: dict[str, str] = {"Content-Type": "application/json"}
            if api_key:
                req_headers["Authorization"] = f"Bearer {api_key}"

            base_payload = {"model": model, "voice": voice}
            client = get_http_client()

            # Step 4+5: Launch all TTS requests concurrently, then yield PCM in segment order.
            # asyncio.gather preserves order and propagates CancelledError naturally.
            try:
                responses = await asyncio.gather(
                    *[
                        client.post(
                            f"{base_url}/v1/audio/speech",
                            headers=req_headers,
                            json={**base_payload, "input": seg},
                            timeout=_TTS_TIMEOUT_SECONDS,
                        )
                        for seg, _ in segments
                    ],
                    return_exceptions=True,
                )
            except asyncio.CancelledError:
                _log.info("TTS interrupted before any request completed")
                raise

            produced_audio = False
            first_frame = True
            for (seg_text, silence_ms), resp in zip(segments, responses, strict=True):
                if isinstance(resp, BaseException):
                    _log.error("TTS HTTP request failed: %s", resp)
                    continue

                if resp.status_code != 200:
                    _log.error("TTS upstream error %d: %s", resp.status_code, resp.text)
                    continue

                try:
                    pcm_bytes, sample_rate, num_channels = _extract_pcm(resp.content)
                except Exception as exc:
                    _log.error("WAV extraction failed: %s", exc)
                    continue

                produced_audio = True
                # 20ms chunks: sample_rate * 0.02 * num_channels * 2 bytes/sample
                chunk_size = int(sample_rate * 0.02) * num_channels * 2

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
                    silence = _make_silence_pcm(silence_ms, sample_rate, num_channels, 2)
                    for i in range(0, len(silence), chunk_size):
                        if first_frame and self._turn_timer:
                            self._turn_timer.mark_first_audio()
                            first_frame = False
                        yield TTSAudioRawFrame(
                            audio=silence[i : i + chunk_size],
                            sample_rate=sample_rate,
                            num_channels=num_channels,
                        )

            if produced_audio:
                outcome = "ok"
            elif any(isinstance(r, httpx.TimeoutException) for r in responses):
                outcome = "timeout"
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        finally:
            get_telemetry().record_pipeline_stage(time.perf_counter() - t0, stage="voice.tts", outcome=outcome)
