"""Taigi Bus Agent Pipecat Processor.

Wraps the existing `AgentSession` into a Pipecat FrameProcessor.
It listens for `TranscriptionFrame` (from STT), feeds the text into the
agent's LLM/tool loop, persists the conversation state, and emits
a `TextFrame` (for TTS) with the agent's reply.
"""

import asyncio
import logging
from collections.abc import Callable
from contextlib import suppress
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


class _ResponseState:
    """Tracks whether one `_run_agent_inference` call has already pushed
    LLMFullResponseStartFrame/TextFrame downstream.

    Passed explicitly into `_run_agent_inference` (not read off `self`) so a
    cancelled task and the task replacing it never race over the same flag —
    each in-flight call owns its own instance.
    """

    __slots__ = ("started",)

    def __init__(self) -> None:
        self.started = False


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
        self._inference_state: _ResponseState | None = None

    def _end_conversation_tool(self) -> tuple[dict, Any]:
        """Build the (schema, handler) pair injected into this session's tools.

        The handler's return string goes back to the LLM as the tool result; the
        model speaks the actual farewell on the following round (the frontend
        waits for bot_silent before closing, so the goodbye audio still plays).
        """

        async def end_conversation() -> str:
            if self._send_event:
                self._send_event({"type": "end_conversation"})
            return "好，對話已標記結束，跟使用者道別即可。"

        return (_END_CONVERSATION_SCHEMA, end_conversation)

    async def _cancel_inference_task(self) -> None:
        """Cancel `self._inference_task` and wait for its cleanup to finish.

        `asyncio.Task.cancel()` only schedules a CancelledError for the next
        time the task resumes — it does not run its except/cleanup code
        synchronously. Firing a bare cancel() and immediately starting a
        replacement task would let the old task's CancelledError handler
        (which may push a stale LLMFullResponseEndFrame, see
        `_run_agent_inference`) race the new task's own
        LLMFullResponseStartFrame with no ordering guarantee, so a stale End
        could land *after* the new Start and break the TTS sentence
        aggregator's Start/End pairing. Awaiting the task here forces its
        cleanup to finish first; it's parked on an I/O await (LLM stream /
        push_frame), so cancellation propagates quickly.
        """
        task = self._inference_task
        if task is None or task.done():
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def cleanup(self):
        """Cancel any in-flight inference before the base class tears us down.

        `FrameProcessor.cleanup()` only cancels the input/process tasks it
        created itself — it doesn't know about the task we spawn with
        `create_task()` in `process_frame`. Without this override, that task
        would only be cancelled by the *next* Transcription/InterruptionFrame,
        which never arrives once the client disconnects mid-inference, leaving
        it running and holding the AgentSession + LLM stream alive forever.

        Ours is cancelled first, while the processor can still flush the frames
        its CancelledError handler pushes; `super().cleanup()` afterwards then
        cancels the machinery those frames travel through.
        """
        await self._cancel_inference_task()
        self._inference_task = None
        await super().cleanup()

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        """Process incoming frames, trigger agent on transcription."""
        await super().process_frame(frame, direction)

        if isinstance(frame, InterruptionFrame):
            if self._inference_task and not self._inference_task.done():
                _log.info("Agent generation interrupted by user")
                await self._cancel_inference_task()
                self._inference_task = None
            await self.push_frame(frame, direction)

        elif isinstance(frame, TranscriptionFrame):
            text = frame.text.strip()
            if not text:
                return

            _log.info("Agent received transcription: %s", text)
            if self._turn_timer:
                self._turn_timer.mark_transcription()

            # Cancel any existing task and wait for its cleanup (see
            # _cancel_inference_task docstring). If it already pushed Start/
            # TextFrame into TTS, a bare .cancel() would only stop further
            # generation while the stale reply's queued audio keeps playing and
            # overlaps the new one — so send our own InterruptionFrame here
            # rather than relying on BargeInProcessor, whose _bot_speaking gate
            # may not have fired yet.
            if self._inference_task and not self._inference_task.done():
                state_was_started = bool(self._inference_state and self._inference_state.started)
                await self._cancel_inference_task()
                if state_was_started:
                    _log.info("Cancelling in-flight response that already pushed frames; sending InterruptionFrame")
                    await self.push_frame(InterruptionFrame(), direction)

            state = _ResponseState()
            self._inference_state = state
            self._inference_task = self.create_task(self._run_agent_inference(text, direction, state))
        else:
            await self.push_frame(frame, direction)

    async def _open_stream(self, text: str, stream_kwargs: dict[str, Any]):
        """Open `respond_in_session_stream`, recreating the session once if it expired.

        The chat session's TTL can expire while this long-lived WebRTC
        connection stays open; without this retry every later utterance would
        hit LookupError and the kiosk would go permanently silent. Returns
        `(stream, first_chunk)` on success, or `None` if the session is still
        gone after recreation — bounded to two attempts, since a second
        LookupError means the store can't find its own freshly-created session.
        """
        from api.chat import get_store, respond_in_session_stream

        for attempt in range(2):
            stream = respond_in_session_stream(self.session_id, text, **stream_kwargs)
            try:
                # LookupError surfaces on the first pull, before any chunk.
                first = await anext(stream, None)
                return stream, first
            except LookupError:
                if attempt == 0:
                    _log.warning("Chat session %s expired; recreating for live voice connection", self.session_id)
                    self.session_id = await asyncio.to_thread(get_store().create)
                else:
                    _log.error("Recreated chat session %s immediately missing", self.session_id)
        return None

    async def _run_agent_inference(self, text: str, direction: FrameDirection, state: _ResponseState | None = None):
        """Run the agent logic in a background task so we don't block process_frame.

        Streams reply chunks straight into the pipeline so pipecat's TTS
        sentence aggregator can start synthesizing the first sentence while the
        LLM is still generating the rest, instead of waiting for the full reply.

        `state` defaults to a fresh `_ResponseState` for callers that don't need
        to observe it; `process_frame` always passes its own so it can check
        `.started` when deciding whether a later cancellation needs an
        explicit InterruptionFrame.
        """
        if state is None:
            state = _ResponseState()
        from agent.prompt import VOICE_END_CONVERSATION_GUIDANCE

        extra_tools = [self._end_conversation_tool()]
        stream_kwargs = {
            "extra_tools": extra_tools,
            "extra_system_prompt": VOICE_END_CONVERSATION_GUIDANCE,
        }

        try:
            if self._send_event:
                self._send_event({"type": "transcript", "text": text, "role": "user"})

            opened = await self._open_stream(text, stream_kwargs)
            if opened is None:
                if self._send_event:
                    self._send_event({"type": "agent_cancelled"})
                return
            stream, first = opened

            parts: list[str] = []
            await self.push_frame(LLMFullResponseStartFrame(), direction)
            state.started = True
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
            if state.started:
                # LLMFullResponseStartFrame already went out — close the pair so
                # downstream aggregators (TTS sentence aggregator, etc.) don't sit
                # on a start with no matching end.
                await self.push_frame(LLMFullResponseEndFrame(), direction)
            raise
        except Exception:
            _log.exception("Agent processing error")
            error_reply = "歹勢，我這馬頭腦有點仔打結，請你閣講一擺。"
            await self.push_frame(LLMFullResponseStartFrame(), direction)
            await self.push_frame(TextFrame(text=error_reply), direction)
            await self.push_frame(LLMFullResponseEndFrame(), direction)
            if self._send_event:
                self._send_event({"type": "agent_reply", "text": error_reply, "role": "assistant"})
