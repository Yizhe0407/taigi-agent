"""Taigi Bus Agent Pipecat Processor.

Wraps the existing `AgentSession` into a Pipecat FrameProcessor.
It listens for `TranscriptionFrame` (from STT), feeds the text into the
agent's LLM/tool loop, persists the conversation state, and emits
a `TextFrame` (for TTS) with the agent's reply.
"""

import asyncio
import logging
from collections.abc import AsyncGenerator, Callable
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

from api.session_store import SessionTombstonedError
from async_lifecycle import AsyncResourceOwner, OwnedAsyncResource, create_lifecycle_task, join_task, run_in_thread

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
        self._response_streams: AsyncResourceOwner[Any] = AsyncResourceOwner("agent response streams")
        self._closing = False
        self._cleanup_task: asyncio.Task[None] | None = None
        self._base_cleanup_complete = False
        self._cleanup_complete = False

    def _end_conversation_tool(self) -> tuple[dict, Any]:
        """Build the (schema, handler) pair injected into this session's tools.

        The handler's return string goes back to the LLM as the tool result; the
        model speaks the actual farewell on the following round (the frontend
        waits for bot_silent before closing, so the goodbye audio still plays).
        """

        async def end_conversation() -> str:
            if not self._closing and self._send_event:
                self._send_event({"type": "end_conversation"})
            return "好，對話已標記結束，跟使用者道別即可。"

        return (_END_CONVERSATION_SCHEMA, end_conversation)

    def _on_inference_done(self, task: asyncio.Task[None]) -> None:
        """Retire success/cancellation; retain a failed task as observable debt."""
        if self._inference_task is not task:
            return
        if task.cancelled():
            self._inference_task = None
            self._inference_state = None
            return
        error = task.exception()
        if error is None:
            self._inference_task = None
            self._inference_state = None

    async def _settle_completed_inference(self) -> None:
        """Surface and retire a completed inference before accepting new work."""
        task = self._inference_task
        if task is None or not task.done():
            return
        try:
            await self.cancel_task(task, timeout=None)
        finally:
            if task.done() and self._inference_task is task:
                self._inference_task = None
                self._inference_state = None

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
        if task is None:
            return
        try:
            await self.cancel_task(task, timeout=None)
        finally:
            if task.done() and self._inference_task is task:
                # The task manager surfaces a physical failure to this caller,
                # so the task no longer represents unobserved debt here.
                self._inference_task = None
                self._inference_state = None

    async def _run_cleanup(self) -> None:
        """Run every independent teardown, retaining successful progress."""
        errors: list[BaseException] = []
        try:
            await self._cancel_inference_task()
        except BaseException as error:  # noqa: BLE001 — continue independent teardown
            errors.append(error)

        try:
            await self._response_streams.aclose()
        except BaseException as error:  # noqa: BLE001 — base cleanup must still run
            errors.append(error)

        if not self._base_cleanup_complete:
            try:
                await super().cleanup()
            except BaseException as error:  # noqa: BLE001 — preserve every cleanup failure
                errors.append(error)
            else:
                self._base_cleanup_complete = True

        if not errors:
            self._cleanup_complete = True
            return
        if len(errors) == 1:
            raise errors[0]
        raise BaseExceptionGroup("Agent processor cleanup failed", errors)

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

        The first call permanently closes the processor's acquisition gate.
        One physical cleanup task owns the complete sequence, so cancellation of
        any caller cannot interrupt it. Failed pieces retain their ownership and
        a later cleanup call retries only the pieces that did not finish.
        """
        if self._cleanup_complete:
            return
        self._closing = True
        task = self._cleanup_task
        if task is None:
            task = create_lifecycle_task(
                self._run_cleanup(),
                name="agent-processor-cleanup",
            )
            self._cleanup_task = task

        try:
            await join_task(task)
        finally:
            if self._cleanup_task is task and task.done():
                self._cleanup_task = None

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        """Process incoming frames, trigger agent on transcription."""
        if self._closing:
            return
        await self._settle_completed_inference()
        await super().process_frame(frame, direction)
        if self._closing:
            return
        await self._settle_completed_inference()

        if isinstance(frame, InterruptionFrame):
            if self._inference_task and not self._inference_task.done():
                _log.info("Agent generation interrupted by user")
                await self._cancel_inference_task()
            if self._closing:
                return
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

            if self._closing:
                return
            state = _ResponseState()
            coroutine = self._run_agent_inference(text, direction, state)
            try:
                task = self.create_task(coroutine)
            except BaseException:
                coroutine.close()
                raise
            self._inference_state = state
            self._inference_task = task
            try:
                task.add_done_callback(self._on_inference_done)
            except BaseException as primary:
                # Callback registration is part of task construction.  A task
                # that the processor cannot retire must not survive a failed
                # process_frame call or be overwritten by the next transcript.
                try:
                    await self.cancel_task(task, timeout=None)
                except BaseException as cleanup_error:
                    raise BaseExceptionGroup(
                        "Agent inference task registration and rollback both failed",
                        [primary, cleanup_error],
                    ) from None
                finally:
                    if self._inference_task is task:
                        self._inference_task = None
                        self._inference_state = None
                raise
        else:
            await self.push_frame(frame, direction)

    async def _open_stream(
        self,
        text: str,
        stream_kwargs: dict[str, Any],
    ) -> tuple[OwnedAsyncResource[Any], AsyncGenerator[str, None], str | None] | None:
        """Open the stream and recover TTL expiry under the same session ID.

        A live WebRTC connection can outlast the durable row's TTL. Recovery is
        bounded to one same-ID recreate plus one retry; an explicit DELETE
        tombstone stops recovery so voice can never resurrect an ended session.
        """
        from api.chat import chat_store_operation, respond_in_session_stream

        await self._response_streams.retry_failed()
        for attempt in range(2):
            acquisition = self._response_streams.begin_acquisition()
            try:
                stream = respond_in_session_stream(self.session_id, text, **stream_kwargs)
            except BaseException:
                self._response_streams.abort_acquisition(acquisition)
                raise
            owned_stream = self._response_streams.finish_acquisition(
                acquisition,
                stream,
                lambda candidate: candidate.aclose(),
            )
            try:
                # LookupError surfaces on the first pull, before any chunk.
                first = await anext(stream, None)
                return owned_stream, stream, first
            except BaseException as error:
                # Ownership transfers to the caller only after the first pull
                # succeeds. Every failure before that point must close the local
                # generator, including cancellation and non-LookupError failures.
                try:
                    await self._response_streams.release(owned_stream)
                except BaseException as close_error:
                    if isinstance(close_error, asyncio.CancelledError) and not self._response_streams.owns(owned_stream):
                        raise
                    raise BaseExceptionGroup(
                        "Agent response stream open and rollback both failed",
                        [error, close_error],
                    ) from None

                if not isinstance(error, LookupError):
                    raise
                if attempt == 0:
                    _log.warning(
                        "Chat session %s expired; recreating the same ID for live voice connection",
                        self.session_id,
                    )
                    try:
                        with chat_store_operation() as store_runtime:
                            await run_in_thread(
                                store_runtime.store.create,
                                self.session_id,
                            )
                    except SessionTombstonedError:
                        _log.info("Voice session %s was explicitly deleted; recovery stopped", self.session_id)
                        return None
                else:
                    _log.error("Recreated chat session %s immediately missing", self.session_id)
        return None

    async def _run_agent_inference(
        self,
        text: str,
        direction: FrameDirection,
        state: _ResponseState | None = None,
    ) -> None:
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

        owned_stream: OwnedAsyncResource[Any] | None = None
        stream: AsyncGenerator[str, None] | None = None
        try:
            try:
                if self._closing:
                    return
                if self._send_event:
                    self._send_event({"type": "transcript", "text": text, "role": "user"})

                opened = await self._open_stream(text, stream_kwargs)
                if opened is None:
                    if self._send_event:
                        self._send_event({"type": "agent_cancelled"})
                    return
                owned_stream, stream, first = opened

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
        except BaseException as inference_error:
            if owned_stream is not None:
                try:
                    await self._response_streams.release(owned_stream)
                except BaseException as close_error:
                    if isinstance(close_error, asyncio.CancelledError) and not self._response_streams.owns(owned_stream):
                        raise
                    raise BaseExceptionGroup(
                        "Agent inference and response stream close both failed",
                        [inference_error, close_error],
                    ) from None
            raise
        else:
            if owned_stream is not None:
                await self._response_streams.release(owned_stream)
