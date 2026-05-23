"""TTS synthesis endpoint.

Pipeline per request:
  1. HanloFlow  — Mandarin → 漢羅混合文字
  2. Taibun     — 漢羅 → Tailo 台羅拼音
  3. TTS HTTP   — Tailo → audio（OpenAI /v1/audio/speech 格式）

回傳原始音訊 bytes，Content-Type 沿用 TTS 服務的回應。
X-Hanlo-Text / X-Tailo-Text header 帶 URL-encoded 轉換結果，供前端 debug 顯示。
"""

from __future__ import annotations

import time
import urllib.parse

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

from agent.telemetry import get_telemetry
from config import Settings
from pipeline.text_processor import process as text_process

router = APIRouter()

_TTS_TIMEOUT_SECONDS = 30


class TTSRequest(BaseModel):
    text: str = Field(min_length=1, max_length=5000)


def _tts_config() -> tuple[str, str, str, str]:
    """Return (base_url, model, voice, api_key). Raise 503 if TTS_BASE_URL not set."""
    settings = Settings.from_env()
    if not settings.tts_base_url:
        raise HTTPException(
            status_code=503,
            detail="TTS 服務尚未設定（TTS_BASE_URL）",
        )
    return (
        settings.tts_base_url.rstrip("/"),
        settings.tts_model,
        settings.tts_voice,
        settings.tts_api_key,
    )


@router.post("/api/tts")
async def synthesize(body: TTSRequest) -> Response:
    """Synthesize Taiwanese Hokkien speech from Mandarin text.

    Steps:
      1. HanloFlow converts Mandarin to 漢羅 mixed script.
      2. Taibun converts 漢羅 to Tailo romanization (number-tone format).
      3. Tailo is sent to an OpenAI-compatible /v1/audio/speech endpoint.

    Response headers:
      X-Hanlo-Text — URL-encoded 漢羅 intermediate (for frontend debug)
      X-Tailo-Text — URL-encoded Tailo romanization (for frontend debug)
    """
    base_url, model, voice, api_key = _tts_config()

    # ── Step 1 + 2: Mandarin → 漢羅 → Tailo ──────────────────────────────────
    # Timed manually: pure Python, no HTTP — HTTPXClientInstrumentor won't cover it.
    # The span appears as a child of the FastAPI request span.
    t0 = time.perf_counter()
    try:
        with get_telemetry().start_span(
            "tts.text_process",
            {"tts.input_chars": len(body.text)},
        ):
            result = text_process(body.text)
        get_telemetry().record_pipeline_stage(
            time.perf_counter() - t0, stage="tts.text_process", outcome="ok"
        )
    except Exception as err:
        get_telemetry().record_pipeline_stage(
            time.perf_counter() - t0, stage="tts.text_process", outcome="error"
        )
        raise HTTPException(
            status_code=500, detail=f"文字轉換失敗：{err}"
        ) from err

    if not result.tailo:
        raise HTTPException(
            status_code=422, detail="無法將輸入文字轉為台語發音"
        )

    # ── Step 3: Tailo → audio ─────────────────────────────────────────────────
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    payload = {
        "model": model,
        "input": result.tailo,
        "voice": voice,
    }

    try:
        async with httpx.AsyncClient(timeout=_TTS_TIMEOUT_SECONDS) as client:
            tts_response = await client.post(
                f"{base_url}/v1/audio/speech",
                headers=headers,
                json=payload,
            )
    except httpx.TimeoutException as err:
        raise HTTPException(status_code=504, detail="TTS 服務逾時") from err
    except httpx.RequestError as err:
        raise HTTPException(
            status_code=503, detail=f"無法連線到 TTS 服務：{err}"
        ) from err

    if tts_response.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"TTS 服務回應錯誤（{tts_response.status_code}）",
        )

    content_type = tts_response.headers.get("content-type", "audio/mpeg")

    return Response(
        content=tts_response.content,
        media_type=content_type,
        headers={
            # URL-encode so Chinese chars are ASCII-safe in HTTP headers
            "X-Hanlo-Text": urllib.parse.quote(result.hanlo[:400]),
            "X-Tailo-Text": urllib.parse.quote(result.tailo[:400]),
        },
    )
