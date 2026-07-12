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

# Voice-only tool. Not registered in agent/tools.py's global TOOL_SCHEMAS: the
# REST frontend has no "end conversation" affordance, and its signal path
# (send_event over the data channel) only exists inside the voice pipeline.
# Injected per-connection via respond_in_session_stream(extra_tools=...).
_END_CONVERSATION_SCHEMA = {
    "type": "function",
    "function": {
        "name": "end_conversation",
        "description": (
            "標記這段對話結束，讓畫面收尾。只在使用者明確表達要結束（講再見、多謝、"
            "按呢就好、毋免矣、我欲走矣…）時才呼叫；呼叫前務必先口語道別。"
            "若意圖不明確、或使用者可能還有問題，不要呼叫。"
        ),
        "parameters": {"type": "object", "properties": {}},
    },
}


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

    def _end_conversation_tool(self) -> tuple[dict, Any]:
        """Build the (schema, handler) pair injected into this session's tools.

        The handler is a closure over `self._send_event` so it can push the
        end-of-conversation signal to the client. It returns a short string that
        goes back to the LLM as the tool result; the model then speaks the actual
        farewell on the following round (the frontend waits for bot_silent before
        closing, so the goodbye audio still plays out).
        """

        async def end_conversation() -> str:
            if self._send_event:
                self._send_event({"type": "end_conversation"})
            return "好，對話已標記結束，跟使用者道別即可。"

        return (_END_CONVERSATION_SCHEMA, end_conversation)

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
        """Run the agent logic in a background task so we don't block process_frame.

        Streams reply chunks straight into the pipeline — Pipecat's TTS
        sentence aggregator starts synthesizing the first sentence while the
        LLM is still generating the rest, instead of waiting for the full
        reply.
        """
        from agent.prompt import VOICE_END_CONVERSATION_GUIDANCE
        from api.chat import _get_store, respond_in_session_stream

        extra_tools = [self._end_conversation_tool()]
        stream_kwargs = {
            "extra_tools": extra_tools,
            "extra_system_prompt": VOICE_END_CONVERSATION_GUIDANCE,
        }

        try:
            if self._send_event:
                self._send_event({"type": "transcript", "text": text, "role": "user"})

            stream = respond_in_session_stream(self.session_id, text, **stream_kwargs)
            try:
                # LookupError surfaces on the first pull, before any chunk.
                first = await anext(stream, None)
            except LookupError:
                # The chat session's TTL expired while this long-lived WebRTC
                # connection stayed open. Recreate a fresh session and retry
                # once — otherwise every later utterance hits LookupError and
                # the kiosk goes permanently silent for a still-connected user.
                _log.warning("Chat session %s expired; recreating for live voice connection", self.session_id)
                self.session_id = await asyncio.to_thread(_get_store().create)
                stream = respond_in_session_stream(self.session_id, text, **stream_kwargs)
                try:
                    first = await anext(stream, None)
                except LookupError:
                    _log.error("Recreated chat session %s immediately missing", self.session_id)
                    if self._send_event:
                        self._send_event({"type": "agent_cancelled"})
                    return

            parts: list[str] = []
            await self.push_frame(LLMFullResponseStartFrame(), direction)
            if first is not None:
                parts.append(first)
                await self.push_frame(TextFrame(text=first), direction)
                async for chunk in stream:
                    parts.append(chunk)
                    await self.push_frame(TextFrame(text=chunk), direction)
            await self.push_frame(LLMFullResponseEndFrame(), direction)

            reply = "".join(parts)
            _log.info("Agent reply: %s", reply)
            if self._send_event:
                self._send_event({"type": "agent_reply", "text": reply, "role": "assistant"})

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
