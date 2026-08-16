"""Tests for TaigiTTSService.run_tts() telemetry (voice.tts pipeline stage + turn latency)."""

import asyncio
import io
import wave
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pipecat.frames.frames import InterruptionFrame, TTSAudioRawFrame
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.tts_service import TTSContext
from pipecat.utils.asyncio.task_manager import TaskManager, TaskManagerParams
from pipecat.utils.base_object import BaseObject

from services.taigi_tts import TTSConfig, TTSConfigError
from voice.tts_taigi import (
    _DECODED_CACHE_MAX_BYTES,
    SubtitleFrame,
    TaigiTTSService,
    _decoded_cache,
    _decoded_cache_clear,
    _decoded_cache_put,
    _decoded_cache_total_bytes,
)


@pytest.fixture(autouse=True)
def _clear_decoded_cache():
    """The decoded-audio LRU cache (finding 4) is process-wide by design so
    repeated fixed text benefits across sessions — but that means it must not
    leak between test cases that reuse "你好" with different mocked outcomes."""
    _decoded_cache_clear()
    yield
    _decoded_cache_clear()


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
        patch(
            "voice.tts_taigi.load_tts_config",
            return_value=TTSConfig("http://localhost", "m", "v", ""),
        ),
        patch("voice.tts_taigi.normalize_for_tts", side_effect=lambda t: t),
        patch("services.taigi_tts.text_process_async", new=AsyncMock(return_value=_FakeTextProcessResult(tailo))),
    )


def test_run_tts_ok_records_ok_outcome_and_marks_turn_timer():
    mock_resp = MagicMock(status_code=200, content=_make_wav_bytes())

    p1, p2, p3 = _patch_common()
    with (
        p1,
        p2,
        p3,
        patch("voice.tts_taigi.synthesize_segments", new=AsyncMock(return_value=[mock_resp])),
        patch("voice.tts_taigi.get_telemetry") as mock_get_telemetry,
    ):
        turn_timer = MagicMock()
        svc = TaigiTTSService(turn_timer=turn_timer)
        frames = asyncio.run(_collect(svc.run_tts("你好", "ctx-1")))

    assert len(frames) > 1
    # SubtitleFrame must precede every audio frame so the frontend can start
    # the reveal animation at the same moment audio starts queuing.
    assert isinstance(frames[0], SubtitleFrame)
    assert frames[0].text == "你好"
    # 160 samples * 2 bytes / (16000 Hz * 1 channel * 2 bytes) * 1000 = 10ms, no punctuation -> no silence.
    assert frames[0].duration_ms == 10
    assert all(isinstance(f, TTSAudioRawFrame) for f in frames[1:])
    turn_timer.mark_first_audio.assert_called_once()
    mock_get_telemetry.return_value.record_pipeline_stage.assert_called_once()
    _, kwargs = mock_get_telemetry.return_value.record_pipeline_stage.call_args
    assert kwargs["stage"] == "voice.tts"
    assert kwargs["outcome"] == "ok"


def test_run_tts_config_error_records_error_outcome():
    with (
        patch("voice.tts_taigi.load_tts_config", side_effect=TTSConfigError("no TTS_BASE_URL")),
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

    p1, p2, p3 = _patch_common()
    with (
        p1,
        p2,
        p3,
        patch("voice.tts_taigi.synthesize_segments", new=AsyncMock(return_value=[mock_resp])),
        patch("voice.tts_taigi.get_telemetry") as mock_get_telemetry,
    ):
        svc = TaigiTTSService()
        frames = asyncio.run(_collect(svc.run_tts("你好", "ctx-1")))

    # All segments failed -> no SubtitleFrame (nothing was actually spoken).
    assert frames == []
    assert not any(isinstance(f, SubtitleFrame) for f in frames)
    _, kwargs = mock_get_telemetry.return_value.record_pipeline_stage.call_args
    assert kwargs["outcome"] == "error"


def test_run_tts_cancelled_mid_request_records_cancelled_outcome():
    """Barge-in cancels the enclosing Task while awaiting gather() — verifies the
    outer except-CancelledError path, not per-item CancelledError (gather with
    return_exceptions=True swallows those as regular results, never reaching here)."""

    async def _slow_post(*args, **kwargs):
        await asyncio.sleep(10)
        raise AssertionError("should have been cancelled before this returns")

    p1, p2, p3 = _patch_common()
    with (
        p1,
        p2,
        p3,
        patch("voice.tts_taigi.synthesize_segments", new=AsyncMock(side_effect=_slow_post)),
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


def test_run_tts_second_call_with_same_text_hits_cache_and_skips_reprocessing():
    """Repeated fixed text (e.g. the welcome greeting) must not re-run
    Hanlo/Taibun conversion or the upstream TTS HTTP call on the second hit."""
    mock_resp = MagicMock(status_code=200, content=_make_wav_bytes())

    p1, p2, p3 = _patch_common()
    with (
        p1,
        p2,
        p3,
        patch("voice.tts_taigi.synthesize_segments", new=AsyncMock(return_value=[mock_resp])) as mock_synth,
        patch("voice.tts_taigi.get_telemetry"),
    ):
        svc = TaigiTTSService()
        first_frames = asyncio.run(_collect(svc.run_tts("你好", "ctx-1")))
        assert mock_synth.call_count == 1

        second_frames = asyncio.run(_collect(svc.run_tts("你好", "ctx-2")))
        # No second upstream synthesis call — served entirely from cache.
        assert mock_synth.call_count == 1

    assert [type(f) for f in first_frames] == [type(f) for f in second_frames]
    assert first_frames[0].duration_ms == second_frames[0].duration_ms


def test_run_tts_same_text_different_voice_does_not_reuse_cached_audio():
    """Regression test for finding 2: the decoded-audio cache key must include
    (model, voice), not just the normalized text — otherwise switching
    TTS_VOICE/TTS_MODEL would keep serving audio synthesized under the old
    config for text that happens to repeat."""
    mock_resp = MagicMock(status_code=200, content=_make_wav_bytes())

    p2, p3 = _patch_common()[1:]
    with (
        p2,
        p3,
        patch("voice.tts_taigi.synthesize_segments", new=AsyncMock(return_value=[mock_resp])) as mock_synth,
        patch("voice.tts_taigi.get_telemetry"),
    ):
        svc = TaigiTTSService()

        with patch("voice.tts_taigi.load_tts_config", return_value=TTSConfig("http://localhost", "m", "voice-a", "")):
            asyncio.run(_collect(svc.run_tts("你好", "ctx-1")))
        assert mock_synth.call_count == 1

        # Same text, different configured voice — must NOT be served from cache.
        with patch("voice.tts_taigi.load_tts_config", return_value=TTSConfig("http://localhost", "m", "voice-b", "")):
            asyncio.run(_collect(svc.run_tts("你好", "ctx-2")))
        assert mock_synth.call_count == 2


def test_run_tts_empty_text_does_not_record_stage():
    """Whitespace-only input is a pure no-op — nothing was attempted, so no stage sample."""
    with patch("voice.tts_taigi.get_telemetry") as mock_get_telemetry:
        svc = TaigiTTSService()
        frames = asyncio.run(_collect(svc.run_tts("   ", "ctx-1")))

    assert frames == []
    mock_get_telemetry.return_value.record_pipeline_stage.assert_not_called()


def _decoded_of(pcm_len: int) -> list:
    """One fake decoded segment holding `pcm_len` bytes of PCM."""
    return [(b"\x00" * pcm_len, 16000, 1, 2, 0)]


def test_decoded_cache_put_evicts_oldest_until_under_byte_budget():
    """Filling the cache past its byte budget must evict the least-recently-used
    entries (not just the newest) until the running total is back under budget."""
    chunk = _DECODED_CACHE_MAX_BYTES // 4

    for i in range(8):
        _decoded_cache_put(f"key-{i}", (_decoded_of(chunk), 100))

    assert _decoded_cache_total_bytes() <= _DECODED_CACHE_MAX_BYTES
    # The most recently inserted entries must have survived the eviction.
    assert "key-7" in _decoded_cache
    assert "key-0" not in _decoded_cache


def test_run_tts_second_call_with_same_text_returns_identical_pcm():
    """A cache hit must yield the exact same PCM bytes as the original synthesis,
    not just the same frame types/count."""
    mock_resp = MagicMock(status_code=200, content=_make_wav_bytes(n_samples=320))

    p1, p2, p3 = _patch_common()
    with (
        p1,
        p2,
        p3,
        patch("voice.tts_taigi.synthesize_segments", new=AsyncMock(return_value=[mock_resp])),
        patch("voice.tts_taigi.get_telemetry"),
    ):
        svc = TaigiTTSService()
        first_frames = asyncio.run(_collect(svc.run_tts("你好", "ctx-1")))
        second_frames = asyncio.run(_collect(svc.run_tts("你好", "ctx-2")))

    first_audio = b"".join(f.audio for f in first_frames if isinstance(f, TTSAudioRawFrame))
    second_audio = b"".join(f.audio for f in second_frames if isinstance(f, TTSAudioRawFrame))
    assert first_audio == second_audio
    assert len(first_audio) > 0


def test_decoded_cache_put_skips_single_entry_larger_than_whole_budget():
    """An oversized single reply (e.g. a long dynamic answer) must not be cached
    at all, and must not wipe out entries already sitting in the cache."""
    _decoded_cache_put("small", (_decoded_of(1024), 100))
    bytes_before = _decoded_cache_total_bytes()

    oversized = _decoded_of(_DECODED_CACHE_MAX_BYTES + 1)
    _decoded_cache_put("huge", (oversized, 100))

    assert "huge" not in _decoded_cache
    assert "small" in _decoded_cache
    assert _decoded_cache_total_bytes() == bytes_before


def test_run_tts_oversized_reply_still_synthesizes_and_plays_when_cache_bypassed():
    """Even when a reply is too large to cache, run_tts() must still synthesize
    it and yield working audio frames — caching is a pure optimization, never
    a precondition for correct playback."""
    huge_samples = (_DECODED_CACHE_MAX_BYTES // 2) + 1  # bytes = samples * 2 (16-bit)
    mock_resp = MagicMock(status_code=200, content=_make_wav_bytes(n_samples=huge_samples))

    p1, p2, p3 = _patch_common()
    with (
        p1,
        p2,
        p3,
        patch("voice.tts_taigi.synthesize_segments", new=AsyncMock(return_value=[mock_resp])) as mock_synth,
        patch("voice.tts_taigi.get_telemetry"),
    ):
        svc = TaigiTTSService()
        frames = asyncio.run(_collect(svc.run_tts("你好", "ctx-1")))
        assert mock_synth.call_count == 1

        # Not cached (bigger than the whole budget) -> a second call re-synthesizes.
        asyncio.run(_collect(svc.run_tts("你好", "ctx-2")))
        assert mock_synth.call_count == 2

    assert isinstance(frames[0], SubtitleFrame)
    assert any(isinstance(f, TTSAudioRawFrame) for f in frames)
    assert _decoded_cache_total_bytes() <= _DECODED_CACHE_MAX_BYTES


async def _service_with_task_manager() -> TaigiTTSService:
    """A TaigiTTSService wired up just enough to run the base class's real
    `_handle_interruption`, which creates/cancels the audio-context task.

    Deliberately not a full `FrameProcessor.setup()` (that needs a clock and a
    live PipelineWorker); only the task manager from `BaseObject.setup` is
    required for the interruption path.
    """
    svc = TaigiTTSService()
    task_manager = TaskManager()
    task_manager.setup(TaskManagerParams(loop=asyncio.get_running_loop()))
    await BaseObject.setup(svc, task_manager)
    return svc


def test_interruption_clears_tts_context_of_interrupted_turn():
    """Barge-in mid-speech must not strand the base class's `_tts_contexts` entry.

    Pipecat only deletes that entry when a TTSStoppedFrame for the context passes
    through push_frame, and every TTSStoppedFrame emit site is gated on
    `push_stop_frames` (False for us) — so before the fix an interrupted turn (the
    common case at a kiosk) left a permanent entry behind. Drives the real
    `TTSService._handle_interruption`, not our override, so the test still covers
    the leak if pipecat changes which hook it calls.
    """

    async def _scenario():
        svc = await _service_with_task_manager()
        # State of a turn whose audio is mid-playback: synthesis registered the
        # TTS context and the audio context queue is still live.
        svc._tts_contexts["ctx-speaking"] = TTSContext()
        svc._audio_contexts["ctx-speaking"] = asyncio.Queue()

        await svc._handle_interruption(InterruptionFrame(), FrameDirection.DOWNSTREAM)
        try:
            assert "ctx-speaking" not in svc._tts_contexts
            assert svc._tts_contexts == {}
        finally:
            # _handle_interruption spawns a fresh audio-context task; stop it so
            # the loop closes without a pending-task warning.
            await svc._stop_audio_context_task()

    asyncio.run(_scenario())


def test_completed_audio_context_only_clears_its_own_tts_context():
    """The cleanup must be scoped to the context that just ended.

    Synthesis runs ahead of playback, so a later turn's entry can already be
    registered while the current context finishes — clearing the whole dict here
    would drop bookkeeping for audio that has not been spoken yet.
    """

    async def _scenario():
        svc = TaigiTTSService()
        svc._tts_contexts["ctx-finished"] = TTSContext()
        svc._tts_contexts["ctx-next-turn"] = TTSContext()

        await svc.on_audio_context_completed(context_id="ctx-finished")

        assert "ctx-finished" not in svc._tts_contexts
        assert "ctx-next-turn" in svc._tts_contexts

    asyncio.run(_scenario())
