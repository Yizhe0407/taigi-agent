"""Shared Taigi TTS configuration, segmentation, and bounded HTTP dispatch.

Also holds the orchestration glue (`prepare_tailo`) shared by the REST
`/api/tts` endpoint (`api/tts.py`) and the voice pipeline's `TaigiTTSService`
(`voice/tts_taigi.py`): both run the same normalize → text-process → split
sequence before diverging on error handling and audio decoding (WAV Response
vs PCM stream), so that middle stretch lives here once instead of twice.
"""

from __future__ import annotations

import asyncio
import math
import os
import re
from dataclasses import dataclass

import httpx

from providers.cloudflare_access import merge_access_headers

from pipeline.text_processor import process_async as text_process_async
from providers.http import get_http_client

TTS_TIMEOUT_SECONDS = 15.0
# Floor for the total synthesize_segments() budget, not a fixed value: a fixed-size
# worker pool runs ceil(segments / TTS_MAX_CONCURRENCY) sequential rounds, and each
# round can legitimately take up to TTS_TIMEOUT_SECONDS. At the segment cap this is
# ceil(64/4) * 15s = 240s. TTS_TOTAL_TIMEOUT_SECONDS only sets the minimum so a
# request with just a couple of segments (one round) isn't held to an artificially
# tiny budget — see `_total_timeout_seconds()`.
TTS_TOTAL_TIMEOUT_SECONDS = 45.0
TTS_MAX_SEGMENTS = 64
TTS_MAX_SEGMENT_CHARS = 500
TTS_MAX_CONCURRENCY = 4

COMMA_SILENCE_MS = 150
PERIOD_SILENCE_MS = 350


class TTSConfigError(RuntimeError):
    """Raised when the TTS upstream is not configured."""


class TTSSegmentLimitError(ValueError):
    """Raised when one request would fan out into too many upstream calls."""


class TTSEmptyResultError(RuntimeError):
    """Raised when text_process_async produced no speakable Tailo output."""


@dataclass(frozen=True)
class TTSConfig:
    base_url: str
    model: str
    voice: str
    api_key: str
    cf_access_client_id: str = ""
    cf_access_client_secret: str = ""


def load_tts_config() -> TTSConfig:
    base_url = os.getenv("TTS_BASE_URL", "").strip()
    if not base_url:
        raise TTSConfigError("TTS 服務尚未設定（TTS_BASE_URL）")
    return TTSConfig(
        base_url=base_url.rstrip("/"),
        model=os.getenv("TTS_MODEL", "tts-1"),
        voice=os.getenv("TTS_VOICE", "taigi"),
        api_key=os.getenv("TTS_API_KEY", ""),
        cf_access_client_id=os.getenv("CF_ACCESS_CLIENT_ID", ""),
        cf_access_client_secret=os.getenv("CF_ACCESS_CLIENT_SECRET", ""),
    )


def _chunk_segment(text: str) -> list[str]:
    chunks: list[str] = []
    remaining = text.strip()
    while len(remaining) > TTS_MAX_SEGMENT_CHARS:
        split_at = remaining.rfind(" ", 0, TTS_MAX_SEGMENT_CHARS + 1)
        if split_at <= 0:
            split_at = TTS_MAX_SEGMENT_CHARS
        chunks.append(remaining[:split_at].strip())
        remaining = remaining[split_at:].strip()
    if remaining:
        chunks.append(remaining)
    return chunks


def split_tailo(tailo: str) -> list[tuple[str, int]]:
    """Split Tailo at punctuation and bound each upstream request's text size."""
    parts = re.split(r"([.,])", tailo.strip())
    result: list[tuple[str, int]] = []
    i = 0
    while i < len(parts):
        segment = parts[i].strip()
        separator = parts[i + 1] if i + 1 < len(parts) else None
        silence_ms = PERIOD_SILENCE_MS if separator == "." else COMMA_SILENCE_MS if separator == "," else 0
        chunks = _chunk_segment(segment)
        result.extend((chunk, silence_ms if index == len(chunks) - 1 else 0) for index, chunk in enumerate(chunks))
        i += 2

    if not result and tailo.strip():
        result = [(tailo.strip(), 0)]
    if len(result) > TTS_MAX_SEGMENTS:
        raise TTSSegmentLimitError(f"TTS 文字切分後共 {len(result)} 段，超過上限 {TTS_MAX_SEGMENTS} 段")
    return result


def make_silence_pcm(duration_ms: int, sample_rate: int, num_channels: int, sample_width: int) -> bytes:
    """Generate raw PCM silence for `duration_ms` at the given audio format."""
    n_frames = int(sample_rate * duration_ms / 1000)
    return b"\x00" * (n_frames * num_channels * sample_width)


@dataclass(frozen=True)
class TailoPreparation:
    """Pre-synthesis intermediate result: 漢羅/Tailo text plus punctuation-split segments.

    Returned before any upstream TTS HTTP call or audio decoding happens —
    callers run `synthesize_segments()` themselves and decode the responses
    into their own output format (WAV Response vs PCM stream).
    """

    hanlo: str
    tailo: str
    segments: list[tuple[str, int]]


async def prepare_tailo(tts_text: str) -> TailoPreparation:
    """Run text_process_async → split_tailo on already-normalized text.

    Shared by `/api/tts` and the voice pipeline's `TaigiTTSService` — both call
    `pipeline.tts_normalizer.normalize_for_tts` themselves first (each needs the
    normalized text independently: the REST endpoint for telemetry span
    attributes, the voice service for its decoded-audio cache key), so
    normalization stays a one-line call at each site rather than folding in here.

    Raises `TTSEmptyResultError` if text_process_async produced nothing to
    speak, or `TTSSegmentLimitError` (from split_tailo) if the result would
    fan out into too many upstream requests. Callers own the error-to-response
    translation (HTTPException vs log-and-drop).
    """
    result = await text_process_async(tts_text)
    if not result.tailo:
        raise TTSEmptyResultError("text_process_async produced empty Tailo output")
    segments = split_tailo(result.tailo)
    return TailoPreparation(hanlo=result.hanlo, tailo=result.tailo, segments=segments)


def _total_timeout_seconds(num_segments: int, worker_count: int) -> float:
    """Total budget for one synthesize_segments() call — see constant comment above."""
    if worker_count <= 0:
        return TTS_TOTAL_TIMEOUT_SECONDS
    rounds = math.ceil(num_segments / worker_count)
    return max(TTS_TOTAL_TIMEOUT_SECONDS, rounds * TTS_TIMEOUT_SECONDS)


async def synthesize_segments(
    config: TTSConfig,
    segments: list[tuple[str, int]],
) -> list[httpx.Response | Exception]:
    """Send segments with a fixed-size worker pool while preserving input order."""
    if len(segments) > TTS_MAX_SEGMENTS:
        raise TTSSegmentLimitError(f"TTS 文字切分後共 {len(segments)} 段，超過上限 {TTS_MAX_SEGMENTS} 段")
    if not segments:
        return []

    headers = {"Content-Type": "application/json"}
    if config.api_key:
        headers["Authorization"] = f"Bearer {config.api_key}"
    headers = merge_access_headers(
        headers,
        client_id=config.cf_access_client_id,
        client_secret=config.cf_access_client_secret,
    )
    base_payload = {"model": config.model, "voice": config.voice}
    client = get_http_client()
    results: list[httpx.Response | Exception | None] = [None] * len(segments)
    next_index = 0

    async def worker() -> None:
        nonlocal next_index
        while next_index < len(segments):
            index = next_index
            next_index += 1
            segment, _ = segments[index]
            try:
                results[index] = await client.post(
                    f"{config.base_url}/v1/audio/speech",
                    headers=headers,
                    json={**base_payload, "input": segment},
                    timeout=TTS_TIMEOUT_SECONDS,
                )
            except Exception as error:  # noqa: BLE001 - returned to caller for uniform handling
                results[index] = error

    worker_count = min(TTS_MAX_CONCURRENCY, len(segments))
    async with asyncio.timeout(_total_timeout_seconds(len(segments), worker_count)):
        await asyncio.gather(*(worker() for _ in range(worker_count)))
    return [result if result is not None else RuntimeError("TTS worker produced no result") for result in results]
