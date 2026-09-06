"""WebRTC voice endpoint with process-lifespan resource ownership."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Coroutine, Iterator
from contextlib import contextmanager
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
from pipecat.transports.smallwebrtc.request_handler import (
    IceCandidate,
    SmallWebRTCPatchRequest,
    SmallWebRTCRequest,
    SmallWebRTCRequestHandler,
)

from agent.diagnostics import log_diagnostic
from api.chat import chat_store_operation
from async_lifecycle import (
    AsyncResourceOwner,
    OwnedAsyncResource,
    cancel_and_join_task,
    create_lifecycle_task,
    join_task,
    run_in_thread,
)
from providers.cloudflare_turn import (
    CloudflareTurnConfigurationError,
    CloudflareTurnUpstreamError,
    TurnIceServers,
    get_turn_ice_servers,
)
from voice.webrtc import OwnedSmallWebRTCConnection

from .request_limits import VOICE_ICE_RATE_LIMIT, VOICE_RATE_LIMIT

router = APIRouter()
_log = logging.getLogger(__name__)


def _raise_primary_and_cleanup(
    message: str,
    primary: BaseException,
    cleanup: BaseException,
) -> None:
    if isinstance(primary, (HTTPException, asyncio.CancelledError)):
        raise primary from cleanup
    raise BaseExceptionGroup(message, [primary, cleanup]) from None


_MAX_RETAINED_PIPELINE_FAILURES = 8


def _raise_errors(message: str, errors: list[BaseException]) -> None:
    if len(errors) == 1:
        raise errors[0]
    if errors:
        raise BaseExceptionGroup(message, errors)


class _VoiceConnectionHandler(SmallWebRTCRequestHandler):
    """Authoritative owner for construction, use, and release of every peer.

    Pipecat's request handler has three lifecycle gaps: it does not roll back a
    partially initialized connection, it swallows callback failures, and it
    registers a new peer only after the callback returns.  This adapter owns the
    complete construction transaction instead of calling that implementation.
    Terminal close permanently shuts the acquisition gate, waits for every
    constructor/renegotiation/ICE operation already in flight, and retains any
    connection whose physical disconnect fails so a later close can retry it.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._connections: AsyncResourceOwner[SmallWebRTCConnection] = AsyncResourceOwner("WebRTC connections")
        self._connection_entries: dict[
            SmallWebRTCConnection,
            OwnedAsyncResource[SmallWebRTCConnection],
        ] = {}

    @property
    def closing(self) -> bool:
        return self._connections.closing

    @property
    def owned_count(self) -> int:
        return self._connections.owned_count

    @property
    def pending_count(self) -> int:
        return self._connections.pending_count

    @property
    def failed_count(self) -> int:
        return self._connections.failed_count

    def _discard_connection(self, connection: SmallWebRTCConnection) -> None:
        if self._pcs_map.get(connection.pc_id) is connection:
            self._pcs_map.pop(connection.pc_id, None)

    async def _disconnect_connection(self, connection: SmallWebRTCConnection) -> None:
        await connection.disconnect()
        self._discard_connection(connection)
        self._connection_entries.pop(connection, None)

    def _prepare_answer(self, connection: SmallWebRTCConnection) -> dict[str, str]:
        answer = connection.get_answer()
        if answer is None:
            raise RuntimeError("SmallWebRTC connection produced no SDP answer")
        if self._esp32_mode:
            from pipecat.runner.utils import smallwebrtc_sdp_munging

            answer["sdp"] = smallwebrtc_sdp_munging(answer["sdp"], self._host)
        return answer

    def _allocate_connection(self) -> OwnedSmallWebRTCConnection:
        return OwnedSmallWebRTCConnection(ice_servers=self._ice_servers)

    def _publish_connection(
        self,
        connection: SmallWebRTCConnection,
        entry: OwnedAsyncResource[SmallWebRTCConnection],
        answer: dict[str, str],
    ) -> None:
        self._connection_entries[connection] = entry
        try:
            self._pcs_map[answer["pc_id"]] = connection
        except BaseException:
            self._connection_entries.pop(connection, None)
            self._discard_connection(connection)
            raise

    async def _rollback_connection(
        self,
        entry: OwnedAsyncResource[SmallWebRTCConnection],
        primary: BaseException,
    ) -> None:
        try:
            await self._connections.release(entry)
        except BaseException as cleanup_error:
            _raise_primary_and_cleanup(
                "WebRTC negotiation and connection rollback both failed",
                primary,
                cleanup_error,
            )
        raise primary

    async def handle_web_request(
        self,
        request: SmallWebRTCRequest,
        webrtc_connection_callback: Callable[[SmallWebRTCConnection], Awaitable[None]],
    ) -> dict[str, str]:
        acquisition = self._connections.begin_acquisition()
        connection: SmallWebRTCConnection | None = None
        entry: OwnedAsyncResource[SmallWebRTCConnection] | None = None
        callback_acquisition: Any | None = None
        callback_entry: OwnedAsyncResource[SmallWebRTCConnection] | None = None

        try:
            pc_id = request.pc_id
            self._check_single_connection_constraints(pc_id)
            existing = self._pcs_map.get(pc_id) if pc_id else None
            if existing is not None:
                try:
                    await existing.renegotiate(
                        sdp=request.sdp,
                        type=request.type,
                        restart_pc=request.restart_pc or False,
                    )
                    return self._prepare_answer(existing)
                finally:
                    self._connections.abort_acquisition(acquisition)

            # Hold a second lease across physical construction and the callback.
            # The first lease is converted into ownership before ``construct``
            # creates an RTCPeerConnection, so even a half-built dependency peer
            # remains reachable by the normal retryable disconnect path.
            callback_acquisition = self._connections.begin_acquisition()
            connection = self._allocate_connection()
            entry = self._connections.finish_acquisition(
                acquisition,
                connection,
                self._disconnect_connection,
            )
            acquisition = None
            callback_entry = entry
            connection.construct()

            @connection.event_handler("closed")
            async def _on_closed(closed: SmallWebRTCConnection) -> None:
                self._discard_connection(closed)
                self._connection_entries.pop(closed, None)
                if callback_entry is not None:
                    self._connections.retire_closed(callback_entry)

            await connection.initialize(sdp=request.sdp, type=request.type)
            if self._connections.closing:
                raise RuntimeError("WebRTC connections owner is closed")
            answer = self._prepare_answer(connection)

            self._publish_connection(connection, entry, answer)

            try:
                await webrtc_connection_callback(connection)
            finally:
                self._connections.abort_acquisition(callback_acquisition)
                callback_acquisition = None
            return answer
        except BaseException as primary:
            if callback_acquisition is not None:
                self._connections.abort_acquisition(callback_acquisition)
            if connection is None:
                if acquisition is not None:
                    self._connections.abort_acquisition(acquisition)
                raise
            if entry is None:
                if acquisition is None:
                    raise RuntimeError("WebRTC construction lost its acquisition lease") from primary
                entry = self._connections.finish_acquisition(
                    acquisition,
                    connection,
                    self._disconnect_connection,
                )
                acquisition = None
                callback_entry = entry
                if getattr(connection, "closed", False):
                    self._connections.retire_closed(entry)
            await self._rollback_connection(entry, primary)
            raise AssertionError("unreachable")

    async def handle_patch_request(self, request: SmallWebRTCPatchRequest) -> None:
        acquisition = self._connections.begin_acquisition()
        try:
            await super().handle_patch_request(request)
        finally:
            self._connections.abort_acquisition(acquisition)

    async def release_connection(self, connection: SmallWebRTCConnection) -> None:
        """Release only a peer still held by this authoritative owner."""
        entry = self._connection_entries.get(connection)
        if entry is None:
            self._discard_connection(connection)
            return
        await self._connections.release(entry)

    async def close(self) -> None:
        await self._connections.aclose()


class _PipelineTaskOwner:
    """Permanent-gate owner for pipeline background tasks and their failures."""

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[None]] = set()
        self._failures: list[BaseException] = []
        self._dropped_failures = 0
        self._closing = False
        self._closed = False
        self._shutdown_task: asyncio.Task[None] | None = None

    @property
    def closing(self) -> bool:
        return self._closing

    @property
    def owned_count(self) -> int:
        return len(self._tasks)

    @property
    def failure_count(self) -> int:
        return len(self._failures)

    @property
    def closed(self) -> bool:
        return self._closed

    def _record_failure(self, error: BaseException) -> None:
        # Retain a summary, never the exception: its traceback pins the peer
        # connection and its args can pin anything else. The full failure was
        # already logged above; shutdown only has to report that it happened.
        if len(self._failures) < _MAX_RETAINED_PIPELINE_FAILURES:
            self._failures.append(RuntimeError(f"{type(error).__name__}: {error}"))
        else:
            self._dropped_failures += 1

    def _record_completion(self, task: asyncio.Task[None]) -> None:
        if task not in self._tasks:
            return
        self._tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is None:
            return
        _log.error(
            "Voice pipeline task failed: %s",
            error,
            exc_info=(type(error), error, error.__traceback__),
        )
        log_diagnostic("voice.pipeline", f"background task crashed: {error}")
        self._record_failure(error)

    async def start(
        self,
        coroutine: Coroutine[Any, Any, None],
        *,
        name: str,
    ) -> asyncio.Task[None]:
        if self._closing:
            coroutine.close()
            raise RuntimeError("Voice pipeline task owner is closed")

        task = create_lifecycle_task(coroutine, name=name)

        self._tasks.add(task)
        try:
            task.add_done_callback(self._record_completion)
        except BaseException as primary:
            try:
                await cancel_and_join_task(task)
            except BaseException as cleanup_error:
                _raise_primary_and_cleanup(
                    "Pipeline task registration and rollback both failed",
                    primary,
                    cleanup_error,
                )
            finally:
                if task.done():
                    self._tasks.discard(task)
            raise
        return task

    async def _finalize(self) -> None:
        errors: list[BaseException] = []
        for task in tuple(self._tasks):
            try:
                if not task.done():
                    await cancel_and_join_task(task)
                # Still owned means its done callback has not run: report here
                # exactly once instead of letting both owners record it.
                if task in self._tasks:
                    try:
                        task.result()
                    except asyncio.CancelledError:
                        pass
            except BaseException as error:  # noqa: BLE001 - settle every active task
                errors.append(error)
            finally:
                if task.done():
                    self._tasks.discard(task)

        errors[0:0] = self._failures
        dropped = self._dropped_failures
        self._failures = []
        self._dropped_failures = 0
        if dropped:
            errors.append(RuntimeError(f"{dropped} further voice pipeline failures were not retained"))

        self._closed = not self._tasks
        _raise_errors("Voice pipeline tasks failed", errors)

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closing = True
        task = self._shutdown_task
        if task is None:
            task = create_lifecycle_task(
                self._finalize(),
                name="voice-pipeline-tasks-shutdown",
            )
            self._shutdown_task = task

        try:
            await join_task(task)
        finally:
            if self._shutdown_task is task and task.done():
                if not self._tasks:
                    self._closed = True
                self._shutdown_task = None


class _VoiceOperationLease:
    """One admitted API operation owned by exactly one voice generation."""

    __slots__ = ("_owner", "_released")

    def __init__(self, owner: _VoiceRuntime) -> None:
        self._owner = owner
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._owner._release_operation(self)


class _VoiceRuntime:
    """Authoritative owner for one process-lifespan voice generation.

    Admission is permanently closed before shutdown waits for every operation
    that already crossed the gate. Only after those continuations have either
    completed or rolled back are pipeline tasks, pipeline runtimes, and peer
    connections closed in dependency order. A failed shutdown keeps this exact
    generation installed and retryable; only an explicit ``startup`` may install
    a successor after physical teardown has succeeded.
    """

    def __init__(self) -> None:
        self.handler = _VoiceConnectionHandler()
        self.pipeline_tasks = _PipelineTaskOwner()
        self.pipeline_runtimes: AsyncResourceOwner[Any] = AsyncResourceOwner("voice pipeline runtimes")
        self._operations: set[_VoiceOperationLease] = set()
        self._operations_empty = asyncio.Event()
        self._operations_empty.set()
        self._closing = False
        self._closed = False
        self._shutdown_task: asyncio.Task[None] | None = None

    @property
    def closing(self) -> bool:
        return self._closing

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def active_operation_count(self) -> int:
        return len(self._operations)

    @property
    def shutdown_task(self) -> asyncio.Task[None] | None:
        return self._shutdown_task

    def require_accepting(self) -> None:
        if self._closing:
            raise HTTPException(status_code=503, detail="Voice service is shutting down")

    def acquire_operation(self) -> _VoiceOperationLease:
        self.require_accepting()
        lease = _VoiceOperationLease(self)
        self._operations.add(lease)
        self._operations_empty.clear()
        return lease

    def _release_operation(self, lease: _VoiceOperationLease) -> None:
        self._operations.discard(lease)
        if not self._operations:
            self._operations_empty.set()

    async def _finalize(self) -> None:
        await self._operations_empty.wait()

        errors: list[BaseException] = []
        for close in (
            self.pipeline_tasks.aclose,
            self.pipeline_runtimes.aclose,
            self.handler.close,
        ):
            try:
                await close()
            except BaseException as error:
                errors.append(error)
        _raise_errors("Voice shutdown failed", errors)

    async def aclose(self) -> None:
        """Join this generation's one cancellation-safe physical teardown."""
        if self._closed:
            return
        self._closing = True
        task = self._shutdown_task
        if task is None:
            task = create_lifecycle_task(self._finalize(), name="voice-shutdown")
            self._shutdown_task = task

        try:
            await join_task(task)
        finally:
            if self._shutdown_task is task and task.done():
                if not task.cancelled() and task.exception() is None:
                    self._closed = True
                self._shutdown_task = None


_voice_runtime: _VoiceRuntime | None = None


def startup() -> None:
    """Install one fresh voice generation after its predecessor fully retired."""
    global _voice_runtime
    previous = _voice_runtime
    if previous is not None and not previous.closed:
        raise RuntimeError("Cannot replace a voice lifecycle before shutdown succeeds")
    _voice_runtime = _VoiceRuntime()


def _require_voice_runtime() -> _VoiceRuntime:
    runtime = _voice_runtime
    if runtime is None:
        raise HTTPException(status_code=503, detail="Voice service is not started")
    runtime.require_accepting()
    return runtime


@contextmanager
def _voice_operation() -> Iterator[_VoiceRuntime]:
    runtime = _require_voice_runtime()
    lease = runtime.acquire_operation()
    try:
        yield runtime
    finally:
        lease.release()


async def _configured_ice_servers(runtime: _VoiceRuntime) -> TurnIceServers:
    """Load and publish ICE configuration within one captured generation."""
    runtime.require_accepting()
    try:
        ice_servers = await get_turn_ice_servers()
    except CloudflareTurnConfigurationError as exc:
        _log.error("Cloudflare TURN is not configured: %s", exc)
        raise HTTPException(status_code=503, detail="WebRTC TURN is not configured") from exc
    except CloudflareTurnUpstreamError as exc:
        _log.warning("Cloudflare TURN credential request failed: %s", exc)
        raise HTTPException(status_code=502, detail="WebRTC TURN is temporarily unavailable") from exc

    runtime.require_accepting()
    runtime.handler.update_ice_servers(list(ice_servers.aiortc))
    return ice_servers


@router.get("/api/voice/ice-servers", dependencies=[Depends(VOICE_ICE_RATE_LIMIT)])
async def webrtc_ice_servers() -> dict:
    with _voice_operation() as runtime:
        ice_servers = await _configured_ice_servers(runtime)
        return {"iceServers": list(ice_servers.browser)}


async def shutdown() -> None:
    """Close the installed generation without clearing failed ownership."""
    runtime = _voice_runtime
    if runtime is None:
        return
    await runtime.aclose()


@router.post("/api/voice/offer", dependencies=[Depends(VOICE_RATE_LIMIT)])
async def webrtc_offer(body: dict) -> dict:
    """Exchange SDP for a voice connection owned by an existing chat session."""
    with _voice_operation() as runtime:
        request_body = dict(body)
        raw_session_id = request_body.pop("session_id", None)
        if raw_session_id is None:
            raise HTTPException(status_code=422, detail="session_id is required")
        try:
            session_id = str(UUID(str(raw_session_id)))
        except (TypeError, ValueError, AttributeError) as exc:
            raise HTTPException(status_code=422, detail="session_id must be a valid UUID") from exc

        try:
            request = SmallWebRTCRequest.from_dict(request_body)
        except (TypeError, KeyError) as exc:
            raise HTTPException(status_code=422, detail=f"Invalid request body: {exc}") from exc

        await _configured_ice_servers(runtime)

        handler = runtime.handler
        task_owner = runtime.pipeline_tasks
        runtime_owner = runtime.pipeline_runtimes

        async def _start_pipeline(connection: SmallWebRTCConnection) -> None:
            runtime.require_accepting()
            from voice.pipeline import run_voice_pipeline

            # A prior runtime whose cleanup failed remains authoritative. A new
            # session may start only after that residual teardown is complete.
            await runtime_owner.retry_failed()
            runtime.require_accepting()

            with chat_store_operation() as store_runtime:
                messages = await run_in_thread(
                    store_runtime.store.load_messages,
                    session_id,
                )
            runtime.require_accepting()
            if messages is None:
                raise HTTPException(status_code=404, detail="對話階段不存在或已過期，請重新開始")
            _log.info("Voice pipeline reusing existing session %s", session_id)

            async def _run_owned_pipeline() -> None:
                try:
                    await run_voice_pipeline(connection, session_id, runtime_owner)
                except BaseException as primary:
                    try:
                        await handler.release_connection(connection)
                    except BaseException as cleanup_error:
                        _raise_primary_and_cleanup(
                            "Voice pipeline and connection release both failed",
                            primary,
                            cleanup_error,
                        )
                    raise
                else:
                    await handler.release_connection(connection)

            runtime.require_accepting()
            await task_owner.start(
                _run_owned_pipeline(),
                name=f"voice-pipeline-{session_id}",
            )

        try:
            return await handler.handle_web_request(request, _start_pipeline)
        except HTTPException:
            raise
        except Exception as exc:
            _log.exception("WebRTC negotiation error")
            raise HTTPException(status_code=500, detail=f"WebRTC negotiation failed: {exc}") from exc


@router.patch("/api/voice/offer", dependencies=[Depends(VOICE_RATE_LIMIT)])
async def webrtc_patch(body: dict) -> None:
    with _voice_operation() as runtime:
        try:
            request = SmallWebRTCPatchRequest(
                pc_id=body["pc_id"],
                candidates=[IceCandidate(**candidate) for candidate in body.get("candidates", [])],
            )
        except (TypeError, KeyError) as exc:
            raise HTTPException(status_code=422, detail=f"Invalid request body: {exc}") from exc

        try:
            await runtime.handler.handle_patch_request(request)
        except HTTPException:
            raise
        except Exception as exc:
            _log.exception("ICE candidate error")
            raise HTTPException(status_code=500, detail=str(exc)) from exc
