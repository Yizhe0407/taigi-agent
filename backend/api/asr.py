"""ASR transcription proxy endpoint."""

from __future__ import annotations

import httpx
from fastapi import APIRouter, HTTPException, Request, UploadFile
from pydantic import BaseModel

from agent.telemetry import get_telemetry
from config import Settings

router = APIRouter()

_ASR_TIMEOUT_SECONDS = 30
_ASR_MAX_BYTES = 25 * 1024 * 1024  # 25 MB — OpenAI Whisper API 上限


def _asr_config() -> tuple[str, str, str]:
    """Return (base_url, model, api_key) from settings, raise 503 if not configured.

    api_key may be empty for self-hosted endpoints that don't require auth.
    """
    settings = Settings.from_env()
    if not settings.asr_base_url or not settings.asr_model:
        raise HTTPException(
            status_code=503, detail="ASR 服務尚未設定（ASR_BASE_URL / ASR_MODEL）"
        )
    return settings.asr_base_url.rstrip("/"), settings.asr_model, settings.asr_api_key


async def _asr_post_audio(
    url: str,
    headers: dict[str, str],
    filename: str,
    audio_bytes: bytes,
    content_type: str,
    model: str,
) -> httpx.Response:
    """Send audio bytes to the ASR endpoint. Extracted for testability."""
    async with httpx.AsyncClient(timeout=_ASR_TIMEOUT_SECONDS) as client:
        return await client.post(
            url,
            headers=headers,
            files={"file": (filename, audio_bytes, content_type)},
            data={"model": model},
        )


class TranscriptionResponse(BaseModel):
    text: str


@router.post("/api/asr", response_model=TranscriptionResponse)
async def transcribe_audio(request: Request, file: UploadFile) -> object:
    """Proxy multipart audio to the Qwen3-ASR endpoint and return transcription text.

    Accepts any audio format the upstream model supports (webm/opus, wav, mp3…).
    Content-Length is checked first so oversized uploads are rejected before the
    body is fully buffered. A second byte-count guard catches chunked uploads that
    arrive without a Content-Length header.
    """
    base_url, model, api_key = _asr_config()

    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > _ASR_MAX_BYTES:
        raise HTTPException(status_code=413, detail="音訊檔案過大（上限 25 MB）")

    audio_bytes = await file.read()
    if len(audio_bytes) > _ASR_MAX_BYTES:
        # Second guard: chunked upload without Content-Length header.
        raise HTTPException(status_code=413, detail="音訊檔案過大（上限 25 MB）")
    if not audio_bytes:
        raise HTTPException(status_code=400, detail="音訊檔案是空的")

    get_telemetry().record_asr_audio_bytes(len(audio_bytes))

    filename = file.filename or "audio.webm"
    content_type = file.content_type or "audio/webm"

    headers: dict[str, str] = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        response = await _asr_post_audio(
            f"{base_url}/v1/audio/transcriptions",
            headers,
            filename,
            audio_bytes,
            content_type,
            model,
        )
    except httpx.TimeoutException as error:
        raise HTTPException(
            status_code=504, detail="語音辨識逾時，請縮短錄音或稍後再試"
        ) from error
    except httpx.RequestError as error:
        raise HTTPException(
            status_code=503, detail=f"無法連線到語音辨識服務：{error}"
        ) from error

    if response.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"語音辨識服務回應錯誤（{response.status_code}）",
        )

    try:
        text: str = response.json().get("text", "").strip()
    except Exception as error:
        raise HTTPException(
            status_code=502, detail="語音辨識服務回應格式錯誤"
        ) from error

    if not text:
        raise HTTPException(status_code=422, detail="未聽清楚，請再說一次")

    return {"text": text}
