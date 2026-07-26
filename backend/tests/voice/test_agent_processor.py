"""Tests for TaigiBusAgentProcessor error-path event emission."""

import asyncio
from unittest.mock import patch

import pytest
from pipecat.frames.frames import InterruptionFrame, LLMFullResponseEndFrame, LLMFullResponseStartFrame, TranscriptionFrame

from voice.agent_processor import TaigiBusAgentProcessor


class _FakeProcessor(TaigiBusAgentProcessor):
    """Minimal subclass: skip FrameProcessor.__init__, stub push_frame."""

    def __init__(self, session_id, send_event):
        self.session_id = session_id
        self._send_event = send_event
        self._inference_task = None
        self._inference_state = None
        self._turn_timer = None
        # process_frame()'s base-class call touches this before our TranscriptionFrame
        # branch runs; TaigiBusAgentProcessor.__init__ is skipped here so it's unset.
        self._observer = None

    async def push_frame(self, frame, direction):
        pass

    def create_task(self, coro):
        return asyncio.ensure_future(coro)


class _FakeStore:
    def create(self):
        return "new-session"


def _raising_stream(error: Exception):
    """A respond_in_session_stream stand-in that raises on the first pull."""

    def factory(sid, msg, **kwargs):
        async def gen():
            raise error
            yield  # pragma: no cover — makes this an async generator

        return gen()

    return factory


def test_lookup_error_sends_agent_cancelled():
    events = []
    proc = _FakeProcessor("sess-1", events.append)

    async def run():
        with (
            patch("api.chat.respond_in_session_stream", _raising_stream(LookupError("sess-1"))),
            patch("api.chat._get_store", lambda: _FakeStore()),
        ):
            await proc._run_agent_inference("test", None)

    asyncio.run(run())
    assert {"type": "agent_cancelled"} in events
    # Session was recreated once before giving up.
    assert proc.session_id == "new-session"


def test_exception_sends_agent_reply_with_error_text():
    events = []
    proc = _FakeProcessor("sess-1", events.append)

    async def run():
        with patch("api.chat.respond_in_session_stream", _raising_stream(RuntimeError("boom"))):
            await proc._run_agent_inference("test", None)

    asyncio.run(run())
    assert any(e.get("type") == "agent_reply" and e.get("role") == "assistant" for e in events)


def test_streamed_chunks_are_pushed_incrementally_and_reply_event_is_full_text():
    """TextFrame chunks still stream into the pipeline (for TTS), but no
    agent_delta events go out anymore — subtitle sync now comes from
    pipeline.py's SubtitleSyncProcessor watching TTSTextFrame instead."""
    events = []
    pushed = []

    class _RecordingProcessor(_FakeProcessor):
        async def push_frame(self, frame, direction):
            pushed.append(frame)

    proc = _RecordingProcessor("sess-1", events.append)

    def fake_stream(sid, msg, **kwargs):
        async def gen():
            yield "第一句。"
            yield "第二句。"

        return gen()

    async def run():
        with patch("api.chat.respond_in_session_stream", fake_stream):
            await proc._run_agent_inference("test", None)

    asyncio.run(run())
    from pipecat.frames.frames import TextFrame

    texts = [f.text for f in pushed if isinstance(f, TextFrame)]
    assert texts == ["第一句。", "第二句。"]
    assert not any(e.get("type") == "agent_delta" for e in events)
    assert {"type": "agent_reply", "text": "第一句。第二句。", "role": "assistant"} in events


def test_injects_end_conversation_tool_whose_handler_emits_event():
    """The voice path must inject an end_conversation tool; its handler pushes
    {"type": "end_conversation"} to the client and returns a str for the LLM."""
    events = []
    proc = _FakeProcessor("sess-1", events.append)
    captured = {}

    def fake_stream(sid, msg, **kwargs):
        captured.update(kwargs)

        async def gen():
            yield "再會啦。"

        return gen()

    async def run():
        with patch("api.chat.respond_in_session_stream", fake_stream):
            await proc._run_agent_inference("再見", None)

    asyncio.run(run())

    # The extra tool was injected with the exact contract name.
    extra_tools = captured["extra_tools"]
    assert [schema["function"]["name"] for schema, _ in extra_tools] == ["end_conversation"]
    assert "extra_system_prompt" in captured and captured["extra_system_prompt"]

    # Its handler emits the frozen JSON contract and returns a str.
    _, handler = extra_tools[0]
    result = asyncio.run(handler())
    assert {"type": "end_conversation"} in events
    assert isinstance(result, str) and result


def test_cancelling_response_that_already_started_sends_interruption_frame():
    """Regression test for the barge-in overlap bug: if the bot hasn't started
    audible playback yet (BargeInProcessor's _bot_speaking gate never fires),
    a new TranscriptionFrame arriving while the previous reply already pushed
    Start/TextFrame downstream must still send an InterruptionFrame — otherwise
    that reply's queued TTS audio plays on top of the new one."""
    pushed = []

    class _RecordingProcessor(_FakeProcessor):
        async def push_frame(self, frame, direction):
            pushed.append(frame)

    proc = _RecordingProcessor("sess-1", None)
    hold = asyncio.Event()

    def fake_stream(sid, msg, **kwargs):
        async def gen():
            yield "第一句。"
            await hold.wait()

        return gen()

    async def run():
        with patch("api.chat.respond_in_session_stream", fake_stream):
            await proc.process_frame(TranscriptionFrame("你好", "user", "t1"), None)
            # Drain the event loop until the background task has pushed
            # Start + the first TextFrame and is parked on hold.wait().
            for _ in range(5):
                await asyncio.sleep(0)
            assert proc._inference_state.started, "setup: first response must have started"
            assert not any(isinstance(f, InterruptionFrame) for f in pushed)

            # A second utterance arrives before the first reply finished.
            await proc.process_frame(TranscriptionFrame("閣再講一擺", "user", "t2"), None)

            hold.set()
            for _ in range(5):
                await asyncio.sleep(0)
            if proc._inference_task and not proc._inference_task.done():
                proc._inference_task.cancel()
                await asyncio.sleep(0)

    asyncio.run(run())
    assert any(isinstance(f, InterruptionFrame) for f in pushed)


def test_cancelled_inference_that_already_started_pushes_end_frame():
    """A cancelled task that already pushed LLMFullResponseStartFrame must also
    push a matching LLMFullResponseEndFrame — mirrors the exception-path
    handling below, which already does this; the CancelledError path didn't."""
    pushed = []

    class _RecordingProcessor(_FakeProcessor):
        async def push_frame(self, frame, direction):
            pushed.append(frame)

    proc = _RecordingProcessor("sess-1", None)

    def fake_stream(sid, msg, **kwargs):
        async def gen():
            yield "第一句。"
            await asyncio.sleep(10)
            yield "unreachable"  # pragma: no cover

        return gen()

    async def run():
        with patch("api.chat.respond_in_session_stream", fake_stream):
            task = asyncio.ensure_future(proc._run_agent_inference("你好", None))
            for _ in range(5):
                await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(run())
    assert any(isinstance(f, LLMFullResponseStartFrame) for f in pushed)
    assert any(isinstance(f, LLMFullResponseEndFrame) for f in pushed)
