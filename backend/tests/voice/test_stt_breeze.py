"""Tests for BreezeSTTService.run_stt()."""

import asyncio
import io
import wave
from unittest.mock import AsyncMock, MagicMock, patch

from pipecat.frames.frames import TranscriptionFrame

from voice.stt_breeze import BreezeSTTService


def _make_wav(sample_rate: int = 16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(b"\x00\x00" * 160)
    return buf.getvalue()


async def _collect(gen):
    return [f async for f in gen]


def test_run_stt_returns_transcription_frame():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"text": "測試"}

    with (
        patch("voice.stt_breeze._asr_config", return_value=("http://localhost", "breeze", None)),
        patch("voice.stt_breeze._asr_post_audio", new=AsyncMock(return_value=mock_resp)),
    ):
        svc = BreezeSTTService()
        frames = asyncio.run(_collect(svc.run_stt(_make_wav())))

    assert len(frames) == 1
    assert isinstance(frames[0], TranscriptionFrame)
    assert frames[0].text == "測試"


def test_run_stt_non_200_yields_nothing():
    mock_resp = MagicMock()
    mock_resp.status_code = 500
    mock_resp.text = "error"

    with (
        patch("voice.stt_breeze._asr_config", return_value=("http://localhost", "breeze", None)),
        patch("voice.stt_breeze._asr_post_audio", new=AsyncMock(return_value=mock_resp)),
    ):
        svc = BreezeSTTService()
        frames = asyncio.run(_collect(svc.run_stt(_make_wav())))

    assert frames == []


def test_run_stt_config_missing_yields_nothing():
    with patch("voice.stt_breeze._asr_config", side_effect=Exception("missing env")):
        svc = BreezeSTTService()
        frames = asyncio.run(_collect(svc.run_stt(_make_wav())))

    assert frames == []
