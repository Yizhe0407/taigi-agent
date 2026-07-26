"""Tests for BreezeSTTService.run_stt()."""

import asyncio
import io
import wave
from unittest.mock import AsyncMock, MagicMock, patch

from pipecat.frames.frames import TranscriptionFrame, TTSSpeakFrame

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


def test_run_stt_non_200_yields_user_facing_apology():
    """Regression test for finding 4: a failed ASR call must not leave the
    user in dead silence — run_stt now yields a TTSSpeakFrame apology so the
    agent still speaks something instead of silently dropping the turn."""
    mock_resp = MagicMock()
    mock_resp.status_code = 500
    mock_resp.text = "error"

    with (
        patch("voice.stt_breeze._asr_config", return_value=("http://localhost", "breeze", None)),
        patch("voice.stt_breeze._asr_post_audio", new=AsyncMock(return_value=mock_resp)),
    ):
        svc = BreezeSTTService()
        frames = asyncio.run(_collect(svc.run_stt(_make_wav())))

    assert len(frames) == 1
    assert isinstance(frames[0], TTSSpeakFrame)
    assert frames[0].text


def test_run_stt_config_missing_yields_user_facing_apology():
    with patch("voice.stt_breeze._asr_config", side_effect=Exception("missing env")):
        svc = BreezeSTTService()
        frames = asyncio.run(_collect(svc.run_stt(_make_wav())))

    assert len(frames) == 1
    assert isinstance(frames[0], TTSSpeakFrame)
    assert frames[0].text


def test_run_stt_request_exception_yields_user_facing_apology():
    """ASR raises (network error, timeout, etc.) — the exact scenario cited in
    finding 4: run_stt's broad `except Exception` used to just log and return,
    leaving the user with no transcript, no reply, and no clue the mic didn't
    catch it."""
    with (
        patch("voice.stt_breeze._asr_config", return_value=("http://localhost", "breeze", None)),
        patch("voice.stt_breeze._asr_post_audio", new=AsyncMock(side_effect=TimeoutError("upstream timed out"))),
    ):
        svc = BreezeSTTService()
        frames = asyncio.run(_collect(svc.run_stt(_make_wav())))

    assert len(frames) == 1
    assert isinstance(frames[0], TTSSpeakFrame)
    assert frames[0].text


def test_run_stt_response_format_error_yields_user_facing_apology():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.side_effect = ValueError("not JSON")

    with (
        patch("voice.stt_breeze._asr_config", return_value=("http://localhost", "breeze", None)),
        patch("voice.stt_breeze._asr_post_audio", new=AsyncMock(return_value=mock_resp)),
    ):
        svc = BreezeSTTService()
        frames = asyncio.run(_collect(svc.run_stt(_make_wav())))

    assert len(frames) == 1
    assert isinstance(frames[0], TTSSpeakFrame)
    assert frames[0].text


def test_run_stt_empty_transcript_yields_nothing():
    """Successful ASR call that heard no speech (silence/noise) is not a
    failure — must stay silent rather than apologizing on every trailing
    silence segment."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"text": "   "}

    with (
        patch("voice.stt_breeze._asr_config", return_value=("http://localhost", "breeze", None)),
        patch("voice.stt_breeze._asr_post_audio", new=AsyncMock(return_value=mock_resp)),
    ):
        svc = BreezeSTTService()
        frames = asyncio.run(_collect(svc.run_stt(_make_wav())))

    assert frames == []
