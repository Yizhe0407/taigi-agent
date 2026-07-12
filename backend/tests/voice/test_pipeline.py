"""Tests for voice/pipeline.py helpers."""

import asyncio
from collections import deque
from unittest.mock import AsyncMock, patch

from voice.pipeline import BargeInProcessor, SubtitleSyncProcessor, TurnLatencyTracker, _drain_chunk_queue

# ---------------------------------------------------------------------------
# _drain_chunk_queue
# ---------------------------------------------------------------------------


def _make_future(loop: asyncio.AbstractEventLoop) -> asyncio.Future:
    return loop.create_future()


def test_drain_chunk_queue_resolves_pending_futures():
    loop = asyncio.new_event_loop()
    try:
        fut1 = loop.create_future()
        fut2 = loop.create_future()
        queue = deque([(b"a", None), (b"b", fut1), (b"c", None), (b"d", fut2)])
        _drain_chunk_queue(queue)
        assert len(queue) == 0
        assert fut1.done() and fut1.result() is True
        assert fut2.done() and fut2.result() is True
    finally:
        loop.close()


def test_drain_chunk_queue_skips_none_futures():
    """None future entries (intermediate chunks) must not raise."""
    loop = asyncio.new_event_loop()
    try:
        queue = deque([(b"x", None), (b"y", None)])
        _drain_chunk_queue(queue)  # should not raise
        assert len(queue) == 0
    finally:
        loop.close()


def test_drain_chunk_queue_skips_already_done_futures():
    loop = asyncio.new_event_loop()
    try:
        fut = loop.create_future()
        fut.set_result(True)
        queue = deque([(b"z", fut)])
        _drain_chunk_queue(queue)  # must not raise InvalidStateError
        assert len(queue) == 0
    finally:
        loop.close()


def test_drain_chunk_queue_empty():
    queue = deque()
    _drain_chunk_queue(queue)  # no-op, no raise
    assert len(queue) == 0


# ---------------------------------------------------------------------------
# BargeInProcessor — bot-speaking gate
# ---------------------------------------------------------------------------
# pipecat FrameProcessor requires a pipeline runtime to call broadcast_interruption()
# (it enqueues onto the pipeline task's internal queue). We test the _bot_speaking
# state machine directly instead of the full interruption path.


def test_barge_in_processor_tracks_bot_speaking_state():
    """BotStartedSpeakingFrame / BotStoppedSpeakingFrame flip the gate flag."""
    from pipecat.frames.frames import BotStartedSpeakingFrame, BotStoppedSpeakingFrame

    async def _run():
        proc = BargeInProcessor()
        proc.push_frame = AsyncMock()
        assert proc._bot_speaking is False

        await proc.process_frame(BotStartedSpeakingFrame(), None)
        assert proc._bot_speaking is True

        await proc.process_frame(BotStoppedSpeakingFrame(), None)
        assert proc._bot_speaking is False

    asyncio.run(_run())


def test_barge_in_processor_no_interruption_when_bot_silent():
    """VADUserStartedSpeakingFrame while bot is silent must NOT call broadcast_interruption."""
    from pipecat.frames.frames import VADUserStartedSpeakingFrame

    async def _run():
        proc = BargeInProcessor()
        proc._bot_speaking = False
        proc.broadcast_interruption = AsyncMock()
        proc.push_frame = AsyncMock()
        await proc.process_frame(VADUserStartedSpeakingFrame(), None)
        proc.broadcast_interruption.assert_not_called()

    asyncio.run(_run())


def test_barge_in_processor_interrupts_when_bot_speaking():
    """VADUserStartedSpeakingFrame while bot is speaking MUST call broadcast_interruption."""
    from pipecat.frames.frames import VADUserStartedSpeakingFrame

    async def _run():
        proc = BargeInProcessor()
        proc._bot_speaking = True
        proc.broadcast_interruption = AsyncMock()
        proc.push_frame = AsyncMock()
        await proc.process_frame(VADUserStartedSpeakingFrame(), None)
        proc.broadcast_interruption.assert_called_once()

    asyncio.run(_run())


def test_barge_in_processor_forwards_bot_speaking_events():
    """BotStartedSpeakingFrame / BotStoppedSpeakingFrame must reach send_event
    as {"type": "bot_speaking"} / {"type": "bot_silent"} for the frontend idle timers."""
    from pipecat.frames.frames import BotStartedSpeakingFrame, BotStoppedSpeakingFrame

    async def _run():
        events = []
        proc = BargeInProcessor(send_event=events.append)
        proc.push_frame = AsyncMock()

        await proc.process_frame(BotStartedSpeakingFrame(), None)
        assert {"type": "bot_speaking"} in events

        await proc.process_frame(BotStoppedSpeakingFrame(), None)
        assert {"type": "bot_silent"} in events

    asyncio.run(_run())


def test_barge_in_processor_forwards_user_speaking_events():
    """VADUserStarted/StoppedSpeakingFrame must reach send_event as
    {"type": "user_speaking"} / {"type": "user_silent"} — the frontend's
    conversation state machine keys its listening/recognizing phases off these."""
    from pipecat.frames.frames import VADUserStartedSpeakingFrame, VADUserStoppedSpeakingFrame

    async def _run():
        events = []
        proc = BargeInProcessor(send_event=events.append)
        proc._bot_speaking = False
        proc.broadcast_interruption = AsyncMock()
        proc.push_frame = AsyncMock()

        await proc.process_frame(VADUserStartedSpeakingFrame(), None)
        await proc.process_frame(VADUserStoppedSpeakingFrame(), None)

        assert events == [{"type": "user_speaking"}, {"type": "user_silent"}]
        # No barge-in while the bot was silent.
        proc.broadcast_interruption.assert_not_called()

    asyncio.run(_run())


def test_barge_in_processor_forwards_user_speaking_even_while_bot_speaks():
    """A barge-in still emits user_speaking AND triggers the interruption."""
    from pipecat.frames.frames import VADUserStartedSpeakingFrame

    async def _run():
        events = []
        proc = BargeInProcessor(send_event=events.append)
        proc._bot_speaking = True
        proc.broadcast_interruption = AsyncMock()
        proc.push_frame = AsyncMock()

        await proc.process_frame(VADUserStartedSpeakingFrame(), None)

        assert {"type": "user_speaking"} in events
        proc.broadcast_interruption.assert_called_once()

    asyncio.run(_run())


def test_barge_in_processor_no_send_event_is_optional():
    """send_event defaults to None (e.g. tests) and must not raise."""
    from pipecat.frames.frames import BotStartedSpeakingFrame

    async def _run():
        proc = BargeInProcessor()
        proc.push_frame = AsyncMock()
        await proc.process_frame(BotStartedSpeakingFrame(), None)  # must not raise
        assert proc._bot_speaking is True

    asyncio.run(_run())


def test_barge_in_processor_records_telemetry_counter():
    """A real barge-in must increment the pipeline.voice.barge_in counter."""
    from pipecat.frames.frames import VADUserStartedSpeakingFrame

    async def _run():
        proc = BargeInProcessor()
        proc._bot_speaking = True
        proc.broadcast_interruption = AsyncMock()
        proc.push_frame = AsyncMock()
        with patch("voice.pipeline.get_telemetry") as mock_get_telemetry:
            await proc.process_frame(VADUserStartedSpeakingFrame(), None)
            mock_get_telemetry.return_value.record_voice_barge_in.assert_called_once()

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# SubtitleSyncProcessor
# ---------------------------------------------------------------------------


def test_subtitle_sync_processor_emits_subtitle_for_subtitle_frame():
    from voice.tts_taigi import SubtitleFrame

    async def _run():
        events = []
        proc = SubtitleSyncProcessor(send_event=events.append)
        proc.push_frame = AsyncMock()

        frame = SubtitleFrame(text="第一句。", duration_ms=850)
        await proc.process_frame(frame, None)

        assert events == [{"type": "subtitle", "text": "第一句。", "durationMs": 850}]
        proc.push_frame.assert_called_once_with(frame, None)

    asyncio.run(_run())


def test_subtitle_sync_processor_passes_other_frames_through_without_event():
    from pipecat.frames.frames import TextFrame

    async def _run():
        events = []
        proc = SubtitleSyncProcessor(send_event=events.append)
        proc.push_frame = AsyncMock()

        frame = TextFrame(text="not a subtitle frame")
        await proc.process_frame(frame, None)

        assert events == []
        proc.push_frame.assert_called_once_with(frame, None)

    asyncio.run(_run())


def test_subtitle_sync_processor_ignores_default_end_of_segment_tts_text_frame():
    """Regression guard: pipecat's own end-of-segment TTSTextFrame (push_text_frames
    default True) must NOT also emit a subtitle event, or subtitles would double up."""
    from pipecat.frames.frames import TTSTextFrame
    from pipecat.utils.text.base_text_aggregator import AggregationType

    async def _run():
        events = []
        proc = SubtitleSyncProcessor(send_event=events.append)
        proc.push_frame = AsyncMock()

        frame = TTSTextFrame(text="第一句。", aggregated_by=AggregationType.TOKEN)
        await proc.process_frame(frame, None)

        assert events == []
        proc.push_frame.assert_called_once_with(frame, None)

    asyncio.run(_run())


def test_subtitle_sync_processor_no_send_event_is_optional():
    from voice.tts_taigi import SubtitleFrame

    async def _run():
        proc = SubtitleSyncProcessor()
        proc.push_frame = AsyncMock()
        # must not raise
        await proc.process_frame(SubtitleFrame(text="x", duration_ms=100), None)

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# TurnLatencyTracker
# ---------------------------------------------------------------------------


def test_turn_latency_tracker_records_duration_on_first_audio():
    with patch("voice.pipeline.get_telemetry") as mock_get_telemetry:
        tracker = TurnLatencyTracker()
        tracker.mark_transcription()
        tracker.mark_first_audio()
        mock_get_telemetry.return_value.record_voice_turn_latency.assert_called_once()
        (duration,) = mock_get_telemetry.return_value.record_voice_turn_latency.call_args[0]
        assert duration >= 0


def test_turn_latency_tracker_noop_without_prior_mark():
    """mark_first_audio() before mark_transcription() must not record (or raise)."""
    with patch("voice.pipeline.get_telemetry") as mock_get_telemetry:
        tracker = TurnLatencyTracker()
        tracker.mark_first_audio()
        mock_get_telemetry.return_value.record_voice_turn_latency.assert_not_called()


def test_turn_latency_tracker_records_once_per_mark():
    """A second mark_first_audio() without a new mark_transcription() must not double-record."""
    with patch("voice.pipeline.get_telemetry") as mock_get_telemetry:
        tracker = TurnLatencyTracker()
        tracker.mark_transcription()
        tracker.mark_first_audio()
        tracker.mark_first_audio()
        assert mock_get_telemetry.return_value.record_voice_turn_latency.call_count == 1
