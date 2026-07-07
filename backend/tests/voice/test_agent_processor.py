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


def test_lookup_error_sends_agent_cancelled():
    events = []
    proc = _FakeProcessor("sess-1", events.append)

    async def fake_respond(sid, msg):
        raise LookupError(sid)

    async def run():
        with patch("api.chat.respond_in_session", fake_respond):
            await proc._run_agent_inference("test", None)

    asyncio.run(run())
    assert {"type": "agent_cancelled"} in events


def test_exception_sends_agent_reply_with_error_text():
    events = []
    proc = _FakeProcessor("sess-1", events.append)

    async def fake_respond(sid, msg):
        raise RuntimeError("boom")

    async def run():
        with patch("api.chat.respond_in_session", fake_respond):
            await proc._run_agent_inference("test", None)

    asyncio.run(run())
    assert any(e.get("type") == "agent_reply" and e.get("role") == "assistant" for e in events)
