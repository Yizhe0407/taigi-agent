"""Taigi Bus Agent Pipecat Processor.

Wraps the existing `AgentSession` into a Pipecat FrameProcessor.
It listens for `TranscriptionFrame` (from STT), feeds the text into the
agent's LLM/tool loop, persists the conversation state, and emits
a `TextFrame` (for TTS) with the agent's reply.
"""

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from pipecat.frames.frames import (
    Frame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    TextFrame,
    TranscriptionFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

_log = logging.getLogger(__name__)


class TaigiBusAgentProcessor(FrameProcessor):
    """Integrates the existing REST AgentSession into the Pipecat pipeline."""

    def __init__(
        self,
        session_id: str,
        send_event: Callable[[Any], None] | None = None,
        turn_timer: Any | None = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.session_id = session_id
        self._send_event = send_event
        self._turn_timer = turn_timer
        self._inference_task: asyncio.Task | None = None

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        """Process incoming frames, trigger agent on transcription."""
        await super().process_frame(frame, direction)

        if isinstance(frame, InterruptionFrame):
            if self._inference_task and not self._inference_task.done():
                _log.info("Agent generation interrupted by user")
                self._inference_task.cancel()
                self._inference_task = None
            await self.push_frame(frame, direction)

        elif isinstance(frame, TranscriptionFrame):
            text = frame.text.strip()
            if not text:
                return

            _log.info("Agent received transcription: %s", text)
            if self._turn_timer:
                self._turn_timer.mark_transcription()

            # Cancel any existing task just in case
            if self._inference_task and not self._inference_task.done():
                self._inference_task.cancel()

            self._inference_task = self.create_task(self._run_agent_inference(text, direction))
        else:
            await self.push_frame(frame, direction)

    async def _run_agent_inference(self, text: str, direction: FrameDirection):
        """Run the agent logic in a background task so we don't block process_frame."""
        from api.chat import respond_in_session

        try:
            if self._send_event:
                self._send_event({"type": "transcript", "text": text, "role": "user"})

            try:
                reply = await respond_in_session(self.session_id, text)
            except LookupError:
                _log.warning("Chat session %s not found", self.session_id)
                if self._send_event:
                    self._send_event({"type": "agent_cancelled"})
                return

            _log.info("Agent reply: %s", reply)
            if self._send_event:
                self._send_event({"type": "agent_reply", "text": reply, "role": "assistant"})
            await self.push_frame(LLMFullResponseStartFrame(), direction)
            await self.push_frame(TextFrame(text=reply), direction)
            await self.push_frame(LLMFullResponseEndFrame(), direction)

        except asyncio.CancelledError:
            _log.info("Agent inference task was cancelled due to interruption.")
            if self._send_event:
                self._send_event({"type": "agent_cancelled"})
            raise
        except Exception:
            _log.exception("Agent processing error")
            error_reply = "歹勢，我這馬頭腦有點仔打結，請你閣講一擺。"
            await self.push_frame(LLMFullResponseStartFrame(), direction)
            await self.push_frame(TextFrame(text=error_reply), direction)
            await self.push_frame(LLMFullResponseEndFrame(), direction)
            if self._send_event:
                self._send_event({"type": "agent_reply", "text": error_reply, "role": "assistant"})
