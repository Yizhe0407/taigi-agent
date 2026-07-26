"""Shared Taigi TTS configuration, segmentation, and bounded HTTP dispatch."""

from __future__ import annotations

import asyncio
import math
import os
import re
from dataclasses import dataclass

import httpx

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


@dataclass(frozen=True)
class TTSConfig:
    base_url: str
    model: str
    voice: str
    api_key: str


def load_tts_config() -> TTSConfig:
    base_url = os.getenv("TTS_BASE_URL", "").strip()
    if not base_url:
        raise TTSConfigError("TTS 服務尚未設定（TTS_BASE_URL）")
    return TTSConfig(
        base_url=base_url.rstrip("/"),
        model=os.getenv("TTS_MODEL", "tts-1"),
        voice=os.getenv("TTS_VOICE", "taigi"),
        api_key=os.getenv("TTS_API_KEY", ""),
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
