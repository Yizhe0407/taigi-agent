"""Tests for TaigiBusAgentProcessor error-path event emission."""

import asyncio
from unittest.mock import patch

from voice.agent_processor import TaigiBusAgentProcessor


class _FakeProcessor(TaigiBusAgentProcessor):
    """Minimal subclass: skip FrameProcessor.__init__, stub push_frame."""

    def __init__(self, session_id, send_event):
        self.session_id = session_id
        self._send_event = send_event
        self._inference_task = None

    async def push_frame(self, frame, direction):
        pass


class _FakeStore:
    def create(self):
        return "new-session"


def _raising_stream(error: Exception):
    """A respond_in_session_stream stand-in that raises on the first pull."""

    def factory(sid, msg):
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

    def fake_stream(sid, msg):
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
