"""TTS synthesis endpoint.

Pipeline per request:
  1. HanloFlow  — Mandarin → 漢羅混合文字
  2. Taibun     — 漢羅 → Tailo 台羅拼音
  3. Split      — Tailo 在 , 和 . 切段
  4. TTS HTTP   — 各段並發送至 /v1/audio/speech，回傳 WAV
  5. Concat     — WAV bytes 拼接，段間插靜音

X-Hanlo-Text / X-Tailo-Text header 帶 URL-encoded 轉換結果，供前端 debug 顯示。
"""

from __future__ import annotations

import io
import time
import urllib.parse
import wave

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

from pipeline.text_processor import process_async as text_process_async
from pipeline.tts_normalizer import normalize_for_tts
from services.taigi_tts import (
    TTSConfigError,
    TTSSegmentLimitError,
    load_tts_config,
    split_tailo,
    synthesize_segments,
)
from telemetry import get_telemetry

from .request_limits import TTS_RATE_LIMIT

router = APIRouter()


class TTSRequest(BaseModel):
    text: str = Field(min_length=1, max_length=5000)


def _make_silence(n_frames: int, n_channels: int, sampwidth: int) -> bytes:
    return b"\x00" * (n_frames * n_channels * sampwidth)


def _concat_wav(wav_bytes_list: list[bytes], silences_ms: list[int]) -> bytes:
    """Concatenate WAV segments with silence between them.

    All segments must share sample rate / channels / sample width — they come
    from the same TTS backend and voice, so a mismatch means an upstream
    format change that needs a code fix, not a value to silently paper over.
    """
    params = None
    pcm_parts: list[bytes] = []

    for i, wav_bytes in enumerate(wav_bytes_list):
        with wave.open(io.BytesIO(wav_bytes)) as wf:
            seg_params = wf.getparams()
            if params is None:
                params = seg_params
            elif (seg_params.framerate, seg_params.nchannels, seg_params.sampwidth) != (
                params.framerate,
                params.nchannels,
                params.sampwidth,
            ):
                raise ValueError(f"TTS segment {i} has mismatched WAV format: {seg_params} vs {params}")
            pcm_parts.append(wf.readframes(wf.getnframes()))
        if i < len(silences_ms) and silences_ms[i] > 0 and params is not None:
            n_frames = int(params.framerate * silences_ms[i] / 1000)
            pcm_parts.append(_make_silence(n_frames, params.nchannels, params.sampwidth))

    if params is None:
        return b""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setparams(params)
        wf.writeframes(b"".join(pcm_parts))
    return buf.getvalue()


@router.post("/api/tts", dependencies=[Depends(TTS_RATE_LIMIT)])
async def synthesize(body: TTSRequest) -> Response:
    """Synthesize Taiwanese Hokkien speech from Mandarin text.

    Steps:
      1. HanloFlow converts Mandarin to 漢羅 mixed script.
      2. Taibun converts 漢羅 to Tailo romanization (number-tone format).
      3. Tailo is split at punctuation; each segment is sent to TTS concurrently.
      4. WAV responses are concatenated with silence between segments.

    Response headers:
      X-Hanlo-Text — URL-encoded 漢羅 intermediate (for frontend debug)
      X-Tailo-Text — URL-encoded Tailo romanization (for frontend debug)
    """
    try:
        config = load_tts_config()
    except TTSConfigError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error

    # ── Step 1 + 2: Mandarin → 漢羅 → Tailo ──────────────────────────────────
    tel = get_telemetry()
    t0 = time.perf_counter()
    try:
        tts_text = normalize_for_tts(body.text)
        with tel.start_span("tts.text_process", {"tts.input_chars": len(tts_text)}) as span:
            result = await text_process_async(tts_text)
            tel.set_content(span, "tts.input_text", tts_text)
            tel.set_content(span, "tts.hanlo_text", result.hanlo)
            tel.set_content(span, "tts.tailo_text", result.tailo)
        outcome = "ok" if result.tailo else "empty_output"
        tel.record_pipeline_stage(time.perf_counter() - t0, stage="tts.text_process", outcome=outcome)
    except Exception as err:
        tel.record_pipeline_stage(time.perf_counter() - t0, stage="tts.text_process", outcome="error")
        raise HTTPException(status_code=500, detail=f"文字轉換失敗：{err}") from err

    if not result.tailo:
        raise HTTPException(status_code=422, detail="無法將輸入文字轉為台語發音")

    # ── Step 3: split Tailo at punctuation ───────────────────────────────────
    try:
        segments = split_tailo(result.tailo)
    except TTSSegmentLimitError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error

    # ── Step 4: concurrent TTS requests ──────────────────────────────────────
    t1 = time.perf_counter()
    # Built alongside validation below (not filtered afterward) so a response's
    # position always matches its segment/silence — a post-hoc `isinstance` filter
    # would silently misalign the two lists if this loop ever tolerates partial
    # failures instead of raising on the first bad segment.
    verified_responses: list[httpx.Response] = []
    try:
        with tel.start_span("tts.synthesize", {"tts.segments": len(segments)}):
            responses = await synthesize_segments(config, segments)
            for resp in responses:
                if isinstance(resp, Exception):
                    raise resp
                if resp.status_code != 200:
                    raise HTTPException(
                        status_code=502,
                        detail=f"TTS 服務回應錯誤（{resp.status_code}）",
                    )
                verified_responses.append(resp)
    except HTTPException:
        tel.record_pipeline_stage(time.perf_counter() - t1, stage="tts.synthesize", outcome="upstream_error")
        raise
    except (httpx.TimeoutException, TimeoutError) as err:
        tel.record_pipeline_stage(time.perf_counter() - t1, stage="tts.synthesize", outcome="timeout")
        raise HTTPException(status_code=504, detail="TTS 服務逾時") from err
    except httpx.RequestError as err:
        tel.record_pipeline_stage(time.perf_counter() - t1, stage="tts.synthesize", outcome="connect_error")
        raise HTTPException(status_code=503, detail=f"無法連線到 TTS 服務：{err}") from err
    tel.record_pipeline_stage(time.perf_counter() - t1, stage="tts.synthesize", outcome="ok")

    # ── Step 5: concatenate WAV with silence ──────────────────────────────────
    silences_ms = [ms for _, ms in segments]
    try:
        audio_bytes = _concat_wav([response.content for response in verified_responses], silences_ms)
    except (ValueError, wave.Error) as err:
        raise HTTPException(status_code=502, detail=f"TTS 音訊格式異常：{err}") from err
    tel.record_tts_audio_bytes(len(audio_bytes))

    return Response(
        content=audio_bytes,
        media_type="audio/wav",
        headers={
            "X-Hanlo-Text": urllib.parse.quote(result.hanlo[:400]),
            "X-Tailo-Text": urllib.parse.quote(result.tailo[:400]),
        },
    )
