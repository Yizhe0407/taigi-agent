"""ASR transcription proxy endpoint."""

from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from pydantic import BaseModel

from providers.asr import ASRConfigError, get_asr_config, post_asr_audio
from telemetry import get_telemetry

from .request_limits import ASR_RATE_LIMIT

router = APIRouter()

_ASR_MAX_BYTES = 25 * 1024 * 1024  # 25 MB — OpenAI Whisper API 上限


class TranscriptionResponse(BaseModel):
    text: str


@router.post("/api/asr", response_model=TranscriptionResponse, dependencies=[Depends(ASR_RATE_LIMIT)])
async def transcribe_audio(request: Request, file: UploadFile) -> object:
    """Proxy multipart audio to the Qwen3-ASR endpoint and return transcription text.

    Accepts any audio format the upstream model supports (webm/opus, wav, mp3…).
    Content-Length is checked first so oversized uploads are rejected before the
    body is fully buffered. A second byte-count guard catches chunked uploads that
    arrive without a Content-Length header.
    """
    try:
        base_url, model, api_key = get_asr_config()
    except ASRConfigError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error

    content_length = request.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > _ASR_MAX_BYTES:
        raise HTTPException(status_code=413, detail="音訊檔案過大（上限 25 MB）")

    chunks: list[bytes] = []
    received = 0
    while chunk := await file.read(1024 * 1024):
        received += len(chunk)
        if received > _ASR_MAX_BYTES:
            raise HTTPException(status_code=413, detail="音訊檔案過大（上限 25 MB）")
        chunks.append(chunk)
    audio_bytes = b"".join(chunks)
    if not audio_bytes:
        raise HTTPException(status_code=400, detail="音訊檔案是空的")

    get_telemetry().record_asr_audio_bytes(len(audio_bytes))

    filename = file.filename or "audio.webm"
    content_type = file.content_type or "audio/webm"

    headers: dict[str, str] = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        response = await post_asr_audio(
            f"{base_url}/v1/audio/transcriptions",
            headers,
            filename,
            audio_bytes,
            content_type,
            model,
        )
    except httpx.TimeoutException as error:
        raise HTTPException(status_code=504, detail="語音辨識逾時，請縮短錄音或稍後再試") from error
    except httpx.RequestError as error:
        raise HTTPException(status_code=503, detail="無法連線到語音辨識服務，請稍後再試") from error

    if response.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"語音辨識服務回應錯誤（{response.status_code}）",
        )

    try:
        text: str = response.json().get("text", "").strip()
    except Exception as error:
        raise HTTPException(status_code=502, detail="語音辨識服務回應格式錯誤") from error

    if not text:
        raise HTTPException(status_code=422, detail="未聽清楚，請再說一次")

    # Transcript onto the FastAPI request span (raw audio is size-only by design).
    get_telemetry().set_current_content("asr.transcript", text)
    return {"text": text}
