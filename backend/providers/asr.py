"""ASR (speech-to-text) upstream provider.

Shared by the REST `/api/asr` proxy (`api/asr.py`) and the voice pipeline's
`BreezeSTTService` (`voice/stt_breeze.py`) — both need the same
config-lookup + multipart-upload call against the OpenAI-compatible
transcription endpoint, so it lives here instead of one importing the
other's private symbols.
"""

from __future__ import annotations

import os

import httpx

from providers.http import get_http_client

ASR_TIMEOUT_SECONDS = 30


class ASRConfigError(RuntimeError):
    """Raised when the ASR upstream is not configured."""


def get_asr_config() -> tuple[str, str, str]:
    """Return (base_url, model, api_key) from env, raise if not configured.

    Read directly from os.getenv so tests can monkeypatch without needing LLM
    vars. api_key may be empty for self-hosted endpoints that don't require auth.
    """
    base_url = os.getenv("ASR_BASE_URL") or ""
    model = os.getenv("ASR_MODEL") or ""
    if not base_url or not model:
        raise ASRConfigError("ASR 服務尚未設定（ASR_BASE_URL / ASR_MODEL）")
    return base_url.rstrip("/"), model, os.getenv("ASR_API_KEY", "")


async def post_asr_audio(
    url: str,
    headers: dict[str, str],
    filename: str,
    audio_bytes: bytes,
    content_type: str,
    model: str,
) -> httpx.Response:
    """Send audio bytes to the ASR endpoint. Extracted for testability."""
    return await get_http_client().post(
        url,
        headers=headers,
        files={"file": (filename, audio_bytes, content_type)},
        data={"model": model},
        timeout=ASR_TIMEOUT_SECONDS,
    )
