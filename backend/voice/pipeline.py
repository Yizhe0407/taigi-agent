"""Pipecat voice pipeline assembly."""

import asyncio
import json
import logging
import time
from collections import deque
from collections.abc import Callable, Coroutine, Sequence
from typing import Any

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    ClientConnectedFrame,
    Frame,
    InterruptionFrame,
    TTSSpeakFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline

# ponytail: PipelineTask (pipeline.task) is deprecated since 1.3.0 → PipelineWorker
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.audio.vad_processor import VADProcessor
from pipecat.processors.frame_processor import (
    FrameDirection,
    FrameProcessor,
    FrameProcessorSetup,
)
from pipecat.transports.base_transport import TransportParams
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
from pipecat.transports.smallwebrtc.transport import SmallWebRTCOutputTransport, SmallWebRTCTransport
from pipecat.utils.asyncio.task_manager import BaseTaskManager, TaskManagerParams
from pipecat.workers.base_worker import WorkerParams

from agent.diagnostics import log_diagnostic
from async_lifecycle import (
    AsyncResourceOwner,
    OwnedAsyncResource,
    cancel_and_join_task,
    create_lifecycle_task,
    join_task,
)
from telemetry import get_telemetry
from voice.agent_processor import TaigiBusAgentProcessor
from voice.stt_breeze import BreezeSTTService
from voice.tts_taigi import SubtitleFrame, TaigiTTSService
from voice.webrtc import clear_event_handlers, dispatch_event_handlers_strict

_log = logging.getLogger(__name__)

_WELCOME_TEXT = "請問您欲前往哪裡？"


class TurnLatencyTracker:
    """Bridges 'user finished speaking -> first TTS audio frame' latency across
    two separate pipeline stages (agent_processor -> tts_taigi).

    Single mutable timestamp slot shared by both processors, so a barge-in
    that starts a new turn mid-measurement can produce a stale/dropped sample.
    Low-stakes metric — fine for now, upgrade to per-turn IDs if it gets noisy.
    """

    def __init__(self) -> None:
        self._t0: float | None = None

    def mark_transcription(self) -> None:
        self._t0 = time.perf_counter()

    def mark_first_audio(self) -> None:
        if self._t0 is None:
            return
        get_telemetry().record_voice_turn_latency(time.perf_counter() - self._t0)
        self._t0 = None


def _drain_chunk_queue(queue: deque) -> None:
    """Drain aiortc RawAudioTrack._chunk_queue, resolving any pending futures.

    deque.clear() would leave in-flight futures un-resolved and their awaiters
    hanging forever, so pop each entry and resolve its future before discarding
    it. Only the last chunk of each add_audio_bytes() call carries a future.
    """
    while queue:
        _, fut = queue.popleft()
        if fut is not None and not fut.done():
            fut.set_result(True)


class BargeInProcessor(FrameProcessor):
    """Convert VADUserStartedSpeakingFrame → broadcast_interruption(), gated on bot speaking.

    PipelineWorker's LLMResponseUniversalAggregator normally does this, but we
    use a custom agent instead of an LLMService, so we need it explicitly.

    Gated on bot-speaking: while the bot is silent (still reasoning/tool-calling,
    nothing pushed downstream yet), a VAD blip from noise/cough would kill
    in-flight work with no recovery path, so this processor stays quiet in that
    case. The barge-in-before-audio-starts race is instead closed at the
    inference level in agent_processor.py, which cancels the prior task on the
    next transcript and sends its own InterruptionFrame if that task had
    already pushed frames downstream.

    Also forwards bot-speaking / user-speaking state to the client over the data
    channel (send_event) so the frontend can drive its playback and listening/
    recognizing UI states. user_speaking fires regardless of the bot-speaking
    gate; only the interruption itself stays gated on the bot currently talking.
    """

    def __init__(self, send_event: Callable[[Any], None] | None = None, **kwargs):
        super().__init__(**kwargs)
        self._bot_speaking = False
        self._send_event = send_event

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, BotStartedSpeakingFrame):
            self._bot_speaking = True
            if self._send_event:
                self._send_event({"type": "bot_speaking"})
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._bot_speaking = False
            if self._send_event:
                self._send_event({"type": "bot_silent"})
        elif isinstance(frame, VADUserStartedSpeakingFrame):
            if self._send_event:
                self._send_event({"type": "user_speaking"})
            if self._bot_speaking:
                _log.debug("Barge-in detected while bot speaking, broadcasting interruption")
                get_telemetry().record_voice_barge_in()
                await self.broadcast_interruption()
        elif isinstance(frame, VADUserStoppedSpeakingFrame):
            if self._send_event:
                self._send_event({"type": "user_silent"})
        await self.push_frame(frame, direction)


class SubtitleSyncProcessor(FrameProcessor):
    """Forwards SubtitleFrames to the client as playback-synced subtitles.

    Placed after transport.output(): BaseOutputTransport's _audio_task_handler
    drains its audio queue at real playback speed and only pushes a frame
    downstream once it's actually queued for playback (a pts-less frame like
    SubtitleFrame queues inline with audio — see tts_taigi.py). So a processor
    here sees each SubtitleFrame at ~the moment its audio starts playing, not
    at LLM-generation time, letting the frontend reveal durationMs of text
    progressively over the real playback window instead of dumping it at once.
    """

    def __init__(self, send_event: Callable[[Any], None] | None = None, **kwargs):
        super().__init__(**kwargs)
        self._send_event = send_event

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, SubtitleFrame) and self._send_event:
            self._send_event({"type": "subtitle", "text": frame.text, "durationMs": frame.duration_ms})
        await self.push_frame(frame, direction)


class _TaigiSmallWebRTCOutputTransport(SmallWebRTCOutputTransport):
    """Extends SmallWebRTCOutputTransport to clear aiortc's audio buffer on barge-in.

    pipecat's handle_interruptions() resets its own _audio_queue, but audio
    already written to aiortc's RawAudioTrack._chunk_queue keeps playing —
    there's no public API to clear it, so override until upstream adds one.
    """

    async def _handle_frame(self, frame: Frame) -> None:
        errors: list[BaseException] = []
        if isinstance(frame, InterruptionFrame):
            if self._client and getattr(self._client, "_audio_output_track", None):
                track = self._client._audio_output_track
                chunk_queue = getattr(track, "_chunk_queue", None)
                # Guard the type (aiortc uses a plain deque here) so a future
                # aiortc refactor doesn't silently no-op instead of erroring.
                if isinstance(chunk_queue, deque):
                    try:
                        _drain_chunk_queue(chunk_queue)
                    except BaseException as error:  # noqa: BLE001 - still forward the frame
                        errors.append(error)
        try:
            await super()._handle_frame(frame)
        except BaseException as error:  # noqa: BLE001 - preserve independent drain failure
            errors.append(error)
        _raise_lifecycle_errors("WebRTC interruption handling failed", errors)


class _TaigiSmallWebRTCTransport(SmallWebRTCTransport):
    """Project-owned transport event and callback lifecycle.

    Pipecat's transport dispatches every event handler in a detached task and
    its client permanently registers four closures on the connection.  This
    adapter executes handlers inline, gates all late events once terminal
    cleanup starts, and removes exactly the client closures installed by this
    transport without disturbing the API owner's connection-close callback.
    """

    def __init__(self, webrtc_connection: SmallWebRTCConnection, *args: Any, **kwargs: Any) -> None:
        self._voice_webrtc_connection = webrtc_connection
        self._voice_event_closing = False
        self._voice_event_closed = False
        self._voice_connection_handlers: dict[str, tuple[Any, ...]] = {}
        self._voice_connection_handlers_detached = False
        baseline = self._snapshot_connection_handlers()
        try:
            super().__init__(webrtc_connection=webrtc_connection, *args, **kwargs)
        except BaseException:
            self._remove_connection_handler_delta(baseline)
            self._voice_event_closing = True
            self._voice_event_closed = True
            event_handlers = getattr(self, "_event_handlers", None)
            if event_handlers is not None:
                clear_event_handlers(self)
            raise
        self._voice_connection_handlers = self._connection_handler_delta(baseline)

    @property
    def closed(self) -> bool:
        return self._voice_event_closed

    @property
    def connection_handlers_detached(self) -> bool:
        return self._voice_connection_handlers_detached

    def _snapshot_connection_handlers(self) -> dict[str, tuple[Any, ...]]:
        handlers = getattr(self._voice_webrtc_connection, "_event_handlers", {})
        return {name: tuple(event.handlers) for name, event in handlers.items()}

    def _connection_handler_delta(
        self,
        baseline: dict[str, tuple[Any, ...]],
    ) -> dict[str, tuple[Any, ...]]:
        current = self._snapshot_connection_handlers()
        delta: dict[str, tuple[Any, ...]] = {}
        for name, handlers in current.items():
            original_ids = {id(handler) for handler in baseline.get(name, ())}
            added = tuple(handler for handler in handlers if id(handler) not in original_ids)
            if added:
                delta[name] = added
        return delta

    def _remove_connection_handler_delta(
        self,
        baseline: dict[str, tuple[Any, ...]],
    ) -> None:
        delta = self._connection_handler_delta(baseline)
        self._remove_connection_handlers(delta)

    def _remove_connection_handlers(self, handlers: dict[str, tuple[Any, ...]]) -> None:
        registered = getattr(self._voice_webrtc_connection, "_event_handlers", {})
        for name, owned_handlers in handlers.items():
            event = registered.get(name)
            if event is None:
                continue
            owned_ids = {id(handler) for handler in owned_handlers}
            event.handlers[:] = [handler for handler in event.handlers if id(handler) not in owned_ids]

    def begin_terminal_cleanup(self) -> None:
        """Permanently stop events and break callback ownership synchronously."""
        self._voice_event_closing = True
        if not self._voice_connection_handlers_detached:
            self._remove_connection_handlers(self._voice_connection_handlers)
            self._voice_connection_handlers.clear()
            self._voice_connection_handlers_detached = True
        clear_event_handlers(self)

    async def _call_event_handler(self, event_name: str, *args: Any, **kwargs: Any) -> None:
        if self._voice_event_closing:
            return
        await dispatch_event_handlers_strict(self, event_name, *args, **kwargs)

    async def _on_app_message(self, message: Any, sender: str) -> None:
        if self._voice_event_closing:
            return
        if self._input:
            await self._input.push_app_message(message)
        if not self._voice_event_closing:
            await self._call_event_handler("on_app_message", message, sender)

    async def _on_client_connected(self, webrtc_connection: SmallWebRTCConnection) -> None:
        if self._voice_event_closing:
            return
        await self._call_event_handler("on_client_connected", webrtc_connection)
        if not self._voice_event_closing and self._input:
            await self._input.push_frame(ClientConnectedFrame())

    async def _on_client_disconnected(self, webrtc_connection: SmallWebRTCConnection) -> None:
        if self._voice_event_closing:
            return
        await self._call_event_handler("on_client_disconnected", webrtc_connection)

    async def cleanup(self) -> None:
        if self._voice_event_closed:
            return
        self.begin_terminal_cleanup()
        await super().cleanup()
        self._voice_event_closed = True

    def output(self) -> _TaigiSmallWebRTCOutputTransport:  # type: ignore[override]
        if not self._output:
            self._output = _TaigiSmallWebRTCOutputTransport(self._client, self._params)
        return self._output  # type: ignore[return-value]


def _raise_lifecycle_errors(message: str, errors: list[BaseException]) -> None:
    if len(errors) == 1:
        raise errors[0]
    if errors:
        raise BaseExceptionGroup(message, errors)


def _raise_primary_and_cleanup(
    message: str,
    primary: BaseException,
    cleanup: BaseException,
) -> None:
    if isinstance(primary, asyncio.CancelledError):
        raise primary from cleanup
    raise BaseExceptionGroup(message, [primary, cleanup]) from None


def _close_unstarted_coroutine(coroutine: Coroutine[Any, Any, Any]) -> None:
    close = getattr(coroutine, "close", None)
    if close is not None:
        close()


class _VoiceTaskManager(BaseTaskManager):
    """Strong, permanent-gate owner for every Pipecat-created asyncio task.

    Pipecat's default task manager wraps tasks in a coroutine that logs and
    consumes ordinary exceptions, and its cancellation path can abandon a task
    after a timeout. A voice runtime instead retains every physical task until
    somebody joins it, never converts cleanup into a timeout, and permanently
    rejects task creation once runtime teardown starts.
    """

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self._closing = False
        self._closed = False
        self._shutdown_task: asyncio.Task[None] | None = None

    def setup(self, params: TaskManagerParams) -> None:
        if self._closing:
            raise RuntimeError("Voice task manager is closed")
        if self._loop is not None and self._loop is not params.loop:
            raise RuntimeError("Voice task manager cannot move to another event loop")
        self._loop = params.loop

    def get_event_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is None:
            raise RuntimeError("Voice task manager is not set up")
        return self._loop

    def _record_completion(self, task: asyncio.Task[Any]) -> None:
        """Retire success/cancellation; retain failure until an owner surfaces it."""
        if task.cancelled():
            self._tasks.discard(task)
            return
        error = task.exception()
        if error is None:
            self._tasks.discard(task)

    def _settle_completed_tasks(self) -> None:
        """Surface every completed failure before admitting another task."""
        errors: list[BaseException] = []
        for task in tuple(self._tasks):
            if not task.done():
                continue
            try:
                task.result()
            except asyncio.CancelledError:
                pass
            except BaseException as error:  # noqa: BLE001 - aggregate independent tasks
                errors.append(error)
            finally:
                self._tasks.discard(task)
        _raise_lifecycle_errors("A previous voice task failed", errors)

    def create_task(
        self,
        coroutine: Coroutine[Any, Any, Any],
        name: str,
    ) -> asyncio.Task[Any]:
        if self._closing:
            _close_unstarted_coroutine(coroutine)
            raise RuntimeError("Voice task manager is closed")

        try:
            self._settle_completed_tasks()
        except BaseException:
            _close_unstarted_coroutine(coroutine)
            raise

        task = create_lifecycle_task(
            coroutine,
            name=name,
            loop=self.get_event_loop(),
        )

        self._tasks.add(task)
        try:
            task.add_done_callback(self._record_completion)
        except BaseException:
            # Synchronous callback registration is part of construction. The
            # manager retains the task and requests cancellation; runtime teardown
            # will cancellation-safely join it even though this call must raise now.
            if not task.done() and task.cancelling() == 0:
                task.cancel()
            raise
        return task

    def current_tasks(self) -> Sequence[asyncio.Task[Any]]:
        return tuple(task for task in self._tasks if not task.done())

    @property
    def closed(self) -> bool:
        return self._closed

    async def cancel_task(
        self,
        task: asyncio.Task[Any],
        timeout: float | None = None,
    ) -> None:
        # ``timeout`` is accepted for Pipecat's interface only. Lifecycle
        # correctness cannot abandon a still-running task after an arbitrary limit.
        del timeout
        try:
            await cancel_and_join_task(task)
        finally:
            if task.done():
                # This caller has now observed either the task's result or its
                # failure, so no failure debt remains in the manager.
                self._tasks.discard(task)

    async def _finalize(self) -> None:
        errors: list[BaseException] = []
        for task in tuple(self._tasks):
            try:
                if task.done():
                    try:
                        task.result()
                    except asyncio.CancelledError:
                        pass
                else:
                    await cancel_and_join_task(task)
            except BaseException as error:  # noqa: BLE001 - drain every independent task
                errors.append(error)
            finally:
                if task.done():
                    self._tasks.discard(task)

        self._closed = not self._tasks
        _raise_lifecycle_errors("Voice task teardown failed", errors)

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closing = True
        task = self._shutdown_task
        if task is None:
            task = create_lifecycle_task(
                self._finalize(),
                name="voice-runtime-tasks-shutdown",
            )
            self._shutdown_task = task

        try:
            await join_task(task)
        finally:
            if self._shutdown_task is task and task.done():
                self._shutdown_task = None


class _RetryableVoicePipeline(Pipeline):
    """Pipeline whose setup rollback and teardown cover every processor.

    Upstream ``Pipeline.cleanup()`` stops at the first failing processor. This
    implementation gives terminal cleanup one physical task, remembers every
    successful processor cleanup, and retries only the failed pieces.
    """

    def __init__(self, processors: Sequence[FrameProcessor]) -> None:
        self._voice_closing = False
        self._voice_closed = False
        self._voice_base_cleaned = False
        self._voice_cleaned_processors: set[FrameProcessor] = set()
        self._voice_cleanup_task: asyncio.Task[None] | None = None
        super().__init__(processors=processors)

    async def setup(self, setup: FrameProcessorSetup) -> None:
        if self._voice_closing:
            raise RuntimeError("Voice pipeline is closed")
        await super().setup(setup)

    @property
    def closed(self) -> bool:
        return self._voice_closed

    async def _run_voice_cleanup(self) -> None:
        errors: list[BaseException] = []

        if not self._voice_base_cleaned:
            try:
                await FrameProcessor.cleanup(self)
            except BaseException as error:  # noqa: BLE001 - continue independent processors
                errors.append(error)
            else:
                self._voice_base_cleaned = True

        for processor in self.processors:
            if processor in self._voice_cleaned_processors:
                continue
            try:
                await processor.cleanup()
            except BaseException as error:  # noqa: BLE001 - cleanup must not fail fast
                errors.append(error)
            else:
                self._voice_cleaned_processors.add(processor)

        self._voice_closed = self._voice_base_cleaned and len(self._voice_cleaned_processors) == len(self.processors)
        _raise_lifecycle_errors("Voice pipeline processor cleanup failed", errors)

    async def cleanup(self) -> None:
        if self._voice_closed:
            return
        self._voice_closing = True
        task = self._voice_cleanup_task
        if task is None:
            task = create_lifecycle_task(
                self._run_voice_cleanup(),
                name="voice-pipeline-processors-shutdown",
            )
            self._voice_cleanup_task = task

        try:
            await join_task(task)
        finally:
            if self._voice_cleanup_task is task and task.done():
                self._voice_cleanup_task = None


class _VoicePipelineWorker(PipelineWorker):
    """Pipecat worker with gap-free setup and retryable independent teardown."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._voice_cleanup_task: asyncio.Task[None] | None = None
        self._voice_close_task: asyncio.Task[None] | None = None
        self._voice_base_cleaned = False
        self._voice_observer_stopped = False
        self._voice_observer_cleaned = False
        self._voice_tracing_ended = False
        self._voice_pipeline_cleanup_required = False
        self._voice_pipeline_cleaned = False
        self._voice_closing = False
        self._voice_closed = False
        super().__init__(*args, **kwargs)

    @property
    def closed(self) -> bool:
        return self._voice_closed

    async def run(self, params: WorkerParams) -> None:
        if self._voice_closing:
            raise RuntimeError("Voice pipeline worker is closed")
        await super().run(params)

    async def _create_tasks(self) -> asyncio.Task[Any]:
        task = await super()._create_tasks()
        try:
            # Upstream waits on ``_finished_event`` before it inspects the main
            # task. Wake that waiter if setup/processing/cleanup crashes first.
            task.add_done_callback(lambda _task: self._finished_event.set())
        except BaseException:
            if not task.done() and task.cancelling() == 0:
                task.cancel()
            raise
        return task

    async def _cancel_tasks(self) -> None:
        errors: list[BaseException] = []
        for attribute in (
            "_process_push_task",
            "_heartbeat_push_task",
            "_heartbeat_monitor_task",
            "_idle_monitor_task",
        ):
            task = getattr(self, attribute, None)
            if task is None:
                continue
            try:
                await self.cancel_task(task, timeout=None)
            except BaseException as error:  # noqa: BLE001 - settle every worker task
                errors.append(error)
            finally:
                if task.done() and getattr(self, attribute, None) is task:
                    setattr(self, attribute, None)

        _raise_lifecycle_errors("Voice worker task teardown failed", errors)

    async def _run_voice_cleanup(self) -> None:
        errors: list[BaseException] = []

        if not self._voice_base_cleaned:
            try:
                await super().cleanup()
            except BaseException as error:  # noqa: BLE001 - continue independent cleanup
                errors.append(error)
            else:
                self._voice_base_cleaned = True

        if not self._voice_observer_stopped:
            try:
                await self._observer.stop()
            except BaseException as error:  # noqa: BLE001
                errors.append(error)
            else:
                self._voice_observer_stopped = True

        if not self._voice_observer_cleaned:
            try:
                await self._observer.cleanup()
            except BaseException as error:  # noqa: BLE001
                errors.append(error)
            else:
                self._voice_observer_cleaned = True

        if not self._voice_tracing_ended:
            try:
                if self._enable_tracing and self._turn_trace_observer:
                    self._turn_trace_observer.end_conversation_tracing()
            except BaseException as error:  # noqa: BLE001
                errors.append(error)
            else:
                self._voice_tracing_ended = True

        if self._voice_pipeline_cleanup_required and not self._voice_pipeline_cleaned:
            try:
                await self._pipeline.cleanup()
            except BaseException as error:  # noqa: BLE001
                errors.append(error)
            else:
                self._voice_pipeline_cleaned = True

        _raise_lifecycle_errors("Voice worker cleanup failed", errors)

    async def _cleanup(self, cleanup_pipeline: bool) -> None:
        self._voice_pipeline_cleanup_required |= cleanup_pipeline
        task = self._voice_cleanup_task
        if task is None:
            task = create_lifecycle_task(
                self._run_voice_cleanup(),
                name="voice-worker-components-shutdown",
            )
            self._voice_cleanup_task = task

        try:
            await join_task(task)
        finally:
            if self._voice_cleanup_task is task and task.done():
                self._voice_cleanup_task = None

        # A concurrent caller may have upgraded a StopFrame cleanup request to a
        # full pipeline cleanup after the physical task took its first snapshot.
        if self._voice_pipeline_cleanup_required and not self._voice_pipeline_cleaned:
            await self._cleanup(cleanup_pipeline=True)

    async def _run_close(self) -> None:
        errors: list[BaseException] = []
        try:
            await self._cancel_tasks()
        except BaseException as error:  # noqa: BLE001
            errors.append(error)
        try:
            await self._cleanup(cleanup_pipeline=True)
        except BaseException as error:  # noqa: BLE001
            errors.append(error)

        self._voice_closed = (
            self._process_push_task is None
            and self._heartbeat_push_task is None
            and self._heartbeat_monitor_task is None
            and self._idle_monitor_task is None
            and self._voice_base_cleaned
            and self._voice_observer_stopped
            and self._voice_observer_cleaned
            and self._voice_tracing_ended
            and self._voice_pipeline_cleaned
        )
        _raise_lifecycle_errors("Voice worker shutdown failed", errors)

    async def aclose(self) -> None:
        if self._voice_closed:
            return
        self._voice_closing = True
        task = self._voice_close_task
        if task is None:
            task = create_lifecycle_task(
                self._run_close(),
                name="voice-worker-shutdown",
            )
            self._voice_close_task = task

        try:
            await join_task(task)
        finally:
            if self._voice_close_task is task and task.done():
                self._voice_close_task = None


class _LooseAsyncCleanup:
    """One constructor-created object not yet transferred to its final owner."""

    def __init__(
        self,
        label: str,
        resource: Any,
        cleanup: Callable[[], Coroutine[Any, Any, None]],
    ) -> None:
        self.label = label
        self.resource = resource
        self.cleanup = cleanup
        self.done = False


class _VoicePipelineRuntime:
    """Authoritative owner for one pipeline's construction, work, and teardown."""

    def __init__(self, connection: SmallWebRTCConnection, session_id: str) -> None:
        self.connection = connection
        self.session_id = session_id
        self._task_manager = _VoiceTaskManager()
        self._transport: _TaigiSmallWebRTCTransport | None = None
        self._pipeline: _RetryableVoicePipeline | None = None
        self._worker: _VoicePipelineWorker | None = None
        self._loose: list[_LooseAsyncCleanup] = []
        self._welcome_task: asyncio.Task[None] | None = None
        self._client_ready = asyncio.Event()
        self._session_active = False
        self._disconnect_handled = False
        self._closing = False
        self._closed = False
        self._cleanup_task: asyncio.Task[None] | None = None

    @property
    def closing(self) -> bool:
        return self._closing

    @property
    def closed(self) -> bool:
        return self._closed

    def _own_loose(self, label: str, resource: Any) -> Any:
        cleanup = getattr(resource, "cleanup", None)
        if cleanup is not None:
            self._loose.append(_LooseAsyncCleanup(label, resource, cleanup))
        return resource

    def _transfer_loose(self, resource: Any) -> None:
        self._loose = [entry for entry in self._loose if entry.resource is not resource]

    def _build(self) -> None:
        if self._closing:
            raise RuntimeError("Voice pipeline runtime is closed")

        transport = self._own_loose(
            "WebRTC transport",
            _TaigiSmallWebRTCTransport(
                webrtc_connection=self.connection,
                params=TransportParams(
                    audio_in_enabled=True,
                    audio_out_enabled=True,
                    video_in_enabled=False,
                    video_out_enabled=False,
                ),
            ),
        )
        self._transport = transport
        self._transfer_loose(transport)

        processors: list[FrameProcessor] = []
        transport_input = self._own_loose("WebRTC input transport", transport.input())
        processors.append(transport_input)

        analyzer = self._own_loose(
            "Silero VAD analyzer",
            SileroVADAnalyzer(params=VADParams(stop_secs=0.7)),
        )
        vad = self._own_loose("VAD processor", VADProcessor(vad_analyzer=analyzer))
        self._transfer_loose(analyzer)
        processors.append(vad)

        barge_in = self._own_loose(
            "barge-in processor",
            BargeInProcessor(send_event=self.connection.send_app_message),
        )
        processors.append(barge_in)

        stt = self._own_loose("STT processor", BreezeSTTService())
        processors.append(stt)

        turn_timer = TurnLatencyTracker()
        agent = self._own_loose(
            "agent processor",
            TaigiBusAgentProcessor(
                session_id=self.session_id,
                send_event=self.connection.send_app_message,
                turn_timer=turn_timer,
            ),
        )
        processors.append(agent)

        tts = self._own_loose("TTS processor", TaigiTTSService(turn_timer=turn_timer))
        processors.append(tts)

        transport_output = self._own_loose("WebRTC output transport", transport.output())
        processors.append(transport_output)

        subtitle_sync = self._own_loose(
            "subtitle processor",
            SubtitleSyncProcessor(send_event=self.connection.send_app_message),
        )
        processors.append(subtitle_sync)

        pipeline = _RetryableVoicePipeline(processors=processors)
        self._pipeline = pipeline
        for processor in processors:
            self._transfer_loose(processor)

        worker = _VoicePipelineWorker(
            pipeline,
            idle_timeout_secs=None,
            params=PipelineParams(),
            task_manager=self._task_manager,
        )
        self._worker = worker
        self._install_event_handlers(transport, worker)

    def _install_event_handlers(
        self,
        transport: _TaigiSmallWebRTCTransport,
        worker: _VoicePipelineWorker,
    ) -> None:
        @transport.event_handler("on_app_message")
        async def on_app_message(_transport, message, _sender) -> None:
            if self._closing:
                return
            try:
                data = json.loads(message) if isinstance(message, (str, bytes)) else message
            except (TypeError, ValueError) as error:
                _log.debug(
                    "Failed to process app message for session %s: %s",
                    self.session_id,
                    error,
                )
                return
            if isinstance(data, dict) and data.get("type") == "client_ready":
                _log.debug("client_ready received for session %s", self.session_id)
                self._client_ready.set()

        @transport.event_handler("on_client_connected")
        async def on_connected(_transport, _connection) -> None:
            if self._closing or self._session_active or self._disconnect_handled:
                return

            get_telemetry().record_voice_session(outcome="connected")
            get_telemetry().record_voice_active_sessions(1)
            self._session_active = True
            self._start_welcome(worker)

        @transport.event_handler("on_client_disconnected")
        async def on_disconnected(_transport, _connection) -> None:
            if self._disconnect_handled:
                return
            self._disconnect_handled = True

            _log.info(
                "WebRTC transport disconnected for session %s — cancelling pipeline",
                self.session_id,
            )
            log_diagnostic(
                "voice.pipeline",
                f"session={self.session_id} disconnected, cancelling pipeline",
            )
            get_telemetry().record_voice_session(outcome="disconnected")
            self._pay_back_active_session()
            if not self._closing:
                await worker.cancel()

    def _start_welcome(self, worker: _VoicePipelineWorker) -> None:
        if self._closing or self._disconnect_handled or self._welcome_task is not None:
            return

        async def _send_welcome() -> None:
            try:
                await asyncio.wait_for(self._client_ready.wait(), timeout=3.0)
            except TimeoutError:
                _log.warning(
                    "client_ready not received within 3 s for session %s — sending welcome anyway",
                    self.session_id,
                )

            if self._closing or self._disconnect_handled:
                return
            self.connection.send_app_message({"type": "agent_reply", "text": _WELCOME_TEXT, "role": "assistant"})
            await worker.queue_frame(TTSSpeakFrame(text=_WELCOME_TEXT))

        task = self._task_manager.create_task(
            _send_welcome(),
            f"voice-welcome-{self.session_id}",
        )
        self._welcome_task = task
        try:
            task.add_done_callback(self._record_welcome_completion)
        except BaseException:
            if not task.done() and task.cancelling() == 0:
                task.cancel()
            raise

    def _record_welcome_completion(self, task: asyncio.Task[Any]) -> None:
        if self._welcome_task is task:
            self._welcome_task = None

    def _pay_back_active_session(self) -> None:
        if not self._session_active:
            return
        get_telemetry().record_voice_active_sessions(-1)
        self._session_active = False

    async def run(self) -> None:
        self._build()
        if self._closing:
            raise RuntimeError("Voice pipeline runtime closed during construction")
        worker = self._worker
        if worker is None:
            raise RuntimeError("Voice pipeline worker was not constructed")

        _log.info(
            "Starting voice pipeline for session_id=%s, pc_id=%s",
            self.session_id,
            self.connection.pc_id,
        )
        self._task_manager.setup(TaskManagerParams(loop=asyncio.get_running_loop()))
        await self.connection.connect()
        if self._closing:
            raise RuntimeError("Voice pipeline runtime closed while connecting")
        await worker.run(WorkerParams(loop=asyncio.get_running_loop()))

    async def _cleanup_loose(self) -> None:
        errors: list[BaseException] = []
        for entry in self._loose:
            if entry.done:
                continue
            try:
                await entry.cleanup()
            except BaseException as error:  # noqa: BLE001 - cleanup every partial object
                errors.append(error)
            else:
                entry.done = True
        self._loose = [entry for entry in self._loose if not entry.done]
        _raise_lifecycle_errors("Partial voice construction cleanup failed", errors)

    async def _run_cleanup(self) -> None:
        errors: list[BaseException] = []

        if self._transport is not None:
            try:
                self._transport.begin_terminal_cleanup()
            except BaseException as error:  # noqa: BLE001
                errors.append(error)

        welcome = self._welcome_task
        if welcome is not None:
            try:
                await self._task_manager.cancel_task(welcome, timeout=None)
            except BaseException as error:  # noqa: BLE001
                errors.append(error)
            finally:
                if welcome.done() and self._welcome_task is welcome:
                    self._welcome_task = None

        if self._worker is not None:
            try:
                await self._worker.aclose()
            except BaseException as error:  # noqa: BLE001
                errors.append(error)
        elif self._pipeline is not None:
            try:
                await self._pipeline.cleanup()
            except BaseException as error:  # noqa: BLE001
                errors.append(error)

        try:
            await self._cleanup_loose()
        except BaseException as error:  # noqa: BLE001
            errors.append(error)

        if self._transport is not None and not self._transport.closed:
            try:
                await self._transport.cleanup()
            except BaseException as error:  # noqa: BLE001
                errors.append(error)

        try:
            await self._task_manager.aclose()
        except BaseException as error:  # noqa: BLE001
            errors.append(error)

        try:
            self._pay_back_active_session()
        except BaseException as error:  # noqa: BLE001
            errors.append(error)

        self._closed = (
            self._welcome_task is None
            and self._task_manager.closed
            and (self._worker is None or self._worker.closed)
            and (self._worker is not None or self._pipeline is None or self._pipeline.closed)
            and not self._loose
            and (self._transport is None or (self._transport.closed and self._transport.connection_handlers_detached))
            and not self._session_active
        )
        _raise_lifecycle_errors("Voice pipeline runtime cleanup failed", errors)

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closing = True
        task = self._cleanup_task
        if task is None:
            task = create_lifecycle_task(
                self._run_cleanup(),
                name=f"voice-runtime-{self.session_id}-shutdown",
            )
            self._cleanup_task = task

        try:
            await join_task(task)
        finally:
            if self._cleanup_task is task and task.done():
                self._cleanup_task = None


async def _close_voice_runtime(runtime: _VoicePipelineRuntime) -> None:
    await runtime.aclose()


async def run_voice_pipeline(
    webrtc_connection: SmallWebRTCConnection,
    session_id: str,
    runtime_owner: AsyncResourceOwner[_VoicePipelineRuntime],
) -> None:
    """Construct and run one runtime behind the process-lifespan owner gate."""
    acquisition = runtime_owner.begin_acquisition()
    runtime: _VoicePipelineRuntime | None = None
    entry: OwnedAsyncResource[_VoicePipelineRuntime] | None = None
    try:
        runtime = _VoicePipelineRuntime(webrtc_connection, session_id)
        entry = runtime_owner.finish_acquisition(
            acquisition,
            runtime,
            _close_voice_runtime,
        )
        acquisition = None
        await runtime.run()
    except BaseException as primary:
        if acquisition is not None:
            runtime_owner.abort_acquisition(acquisition)
        if entry is None:
            raise
        try:
            await runtime_owner.release(entry)
        except BaseException as cleanup_error:
            _raise_primary_and_cleanup(
                "Voice pipeline and runtime cleanup both failed",
                primary,
                cleanup_error,
            )
        raise
    else:
        await runtime_owner.release(entry)
    finally:
        _log.info(
            "Voice pipeline stopped for session_id=%s, pc_id=%s",
            session_id,
            webrtc_connection.pc_id,
        )
