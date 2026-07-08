"""Tests for TaigiTTSService.run_tts() telemetry (voice.tts pipeline stage + turn latency)."""

import asyncio
import io
import wave
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from voice.tts_taigi import TaigiTTSService


def _make_wav_bytes(sample_rate: int = 16000, n_samples: int = 160) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(b"\x00\x00" * n_samples)
    return buf.getvalue()


class _FakeTextProcessResult:
    def __init__(self, tailo: str):
        self.tailo = tailo
        self.hanlo = tailo


async def _collect(gen):
    return [f async for f in gen]


def _patch_common(tailo="li2 ho2"):
    return (
        patch("voice.tts_taigi._tts_config", return_value=("http://localhost", "m", "v", None)),
        patch("voice.tts_taigi.normalize_for_tts", side_effect=lambda t: t),
        patch("voice.tts_taigi.text_process_async", new=AsyncMock(return_value=_FakeTextProcessResult(tailo))),
    )


def test_run_tts_ok_records_ok_outcome_and_marks_turn_timer():
    mock_resp = MagicMock(status_code=200, content=_make_wav_bytes())
    mock_client = MagicMock()
    mock_client.post = AsyncMock(return_value=mock_resp)

    p1, p2, p3 = _patch_common()
    with (
        p1,
        p2,
        p3,
        patch("voice.tts_taigi.get_http_client", return_value=mock_client),
        patch("voice.tts_taigi.get_telemetry") as mock_get_telemetry,
    ):
        turn_timer = MagicMock()
        svc = TaigiTTSService(turn_timer=turn_timer)
        frames = asyncio.run(_collect(svc.run_tts("你好", "ctx-1")))

    assert len(frames) > 0
    turn_timer.mark_first_audio.assert_called_once()
    mock_get_telemetry.return_value.record_pipeline_stage.assert_called_once()
    _, kwargs = mock_get_telemetry.return_value.record_pipeline_stage.call_args
    assert kwargs["stage"] == "voice.tts"
    assert kwargs["outcome"] == "ok"


def test_run_tts_config_error_records_error_outcome():
    with (
        patch("voice.tts_taigi._tts_config", side_effect=Exception("no TTS_BASE_URL")),
        patch("voice.tts_taigi.get_telemetry") as mock_get_telemetry,
    ):
        turn_timer = MagicMock()
        svc = TaigiTTSService(turn_timer=turn_timer)
        frames = asyncio.run(_collect(svc.run_tts("你好", "ctx-1")))

    assert frames == []
    turn_timer.mark_first_audio.assert_not_called()
    _, kwargs = mock_get_telemetry.return_value.record_pipeline_stage.call_args
    assert kwargs["stage"] == "voice.tts"
    assert kwargs["outcome"] == "error"


def test_run_tts_all_segments_upstream_error_records_error_outcome():
    mock_resp = MagicMock(status_code=500, text="boom")
    mock_client = MagicMock()
    mock_client.post = AsyncMock(return_value=mock_resp)

    p1, p2, p3 = _patch_common()
    with (
        p1,
        p2,
        p3,
        patch("voice.tts_taigi.get_http_client", return_value=mock_client),
        patch("voice.tts_taigi.get_telemetry") as mock_get_telemetry,
    ):
        svc = TaigiTTSService()
        frames = asyncio.run(_collect(svc.run_tts("你好", "ctx-1")))

    assert frames == []
    _, kwargs = mock_get_telemetry.return_value.record_pipeline_stage.call_args
    assert kwargs["outcome"] == "error"


def test_run_tts_cancelled_mid_request_records_cancelled_outcome():
    """Barge-in cancels the enclosing Task while awaiting gather() — verifies the
    outer except-CancelledError path, not per-item CancelledError (gather with
    return_exceptions=True swallows those as regular results, never reaching here)."""

    async def _slow_post(*args, **kwargs):
        await asyncio.sleep(10)
        raise AssertionError("should have been cancelled before this returns")

    mock_client = MagicMock()
    mock_client.post = AsyncMock(side_effect=_slow_post)

    p1, p2, p3 = _patch_common()
    with (
        p1,
        p2,
        p3,
        patch("voice.tts_taigi.get_http_client", return_value=mock_client),
        patch("voice.tts_taigi.get_telemetry") as mock_get_telemetry,
    ):
        svc = TaigiTTSService()

        async def _run():
            task = asyncio.ensure_future(_collect(svc.run_tts("你好", "ctx-1")))
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        asyncio.run(_run())

    _, kwargs = mock_get_telemetry.return_value.record_pipeline_stage.call_args
    assert kwargs["stage"] == "voice.tts"
    assert kwargs["outcome"] == "cancelled"


def test_run_tts_empty_text_does_not_record_stage():
    """Whitespace-only input is a pure no-op — nothing was attempted, so no stage sample."""
    with patch("voice.tts_taigi.get_telemetry") as mock_get_telemetry:
        svc = TaigiTTSService()
        frames = asyncio.run(_collect(svc.run_tts("   ", "ctx-1")))

    assert frames == []
    mock_get_telemetry.return_value.record_pipeline_stage.assert_not_called()
