"""Strict ownership tests for the project-owned voice runtime."""

import asyncio
from collections import deque
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

import pytest
from pipecat.frames.frames import InterruptionFrame
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.transports.base_transport import TransportParams
from pipecat.transports.smallwebrtc.transport import SmallWebRTCOutputTransport, SmallWebRTCTransport
from pipecat.utils.asyncio.task_manager import TaskManagerParams

from async_lifecycle import AsyncResourceOwner
from voice.pipeline import (
    _RetryableVoicePipeline,
    _TaigiSmallWebRTCOutputTransport,
    _TaigiSmallWebRTCTransport,
    _VoicePipelineRuntime,
    _VoiceTaskManager,
    run_voice_pipeline,
)


class _FakeTransport:
    def __init__(self) -> None:
        self.handlers: dict[str, Callable[..., Any]] = {}
        self.closing = False
        self.closed = False
        self.connection_handlers_detached = False
        self.cleanup_calls = 0

    def event_handler(self, name: str):
        def decorator(handler):
            self.handlers[name] = handler
            return handler

        return decorator

    def begin_terminal_cleanup(self) -> None:
        self.closing = True
        self.connection_handlers_detached = True
        self.handlers.clear()

    async def cleanup(self) -> None:
        if self.closed:
            return
        self.cleanup_calls += 1
        self.begin_terminal_cleanup()
        self.closed = True


class _FakeWorker:
    def __init__(self, *, block_welcome: bool = False, close_failures: int = 0) -> None:
        self.run_entered = asyncio.Event()
        self.queue_frame_entered = asyncio.Event()
        self.queue_cancel_started = asyncio.Event()
        self.queue_cancel_finished = asyncio.Event()
        self.queue_cancel_release = asyncio.Event()
        self.queue_cancel_release.set()
        self.run_gate = asyncio.get_running_loop().create_future()
        self.block_welcome = block_welcome
        self.close_failures = close_failures
        self.close_calls = 0
        self.cancel_calls = 0
        self.queue_calls = 0
        self.closed = False

    async def run(self, _params) -> None:
        self.run_entered.set()
        await self.run_gate

    async def queue_frame(self, _frame) -> None:
        self.queue_calls += 1
        self.queue_frame_entered.set()
        if not self.block_welcome:
            return
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            self.queue_cancel_started.set()
            await self.queue_cancel_release.wait()
            self.queue_cancel_finished.set()
            raise

    async def cancel(self) -> None:
        self.cancel_calls += 1
        self.finish_run()

    async def aclose(self) -> None:
        self.close_calls += 1
        self.finish_run()
        if self.close_calls <= self.close_failures:
            raise RuntimeError("worker cleanup failed")
        self.closed = True

    def finish_run(self) -> None:
        if not self.run_gate.done():
            self.run_gate.set_result(None)

    def fail_run(self, error: BaseException) -> None:
        if not self.run_gate.done():
            self.run_gate.set_exception(error)


class _FakeConnection:
    pc_id = "pc-test"

    def __init__(self, *, fire_connected: bool = False) -> None:
        self.fire_connected = fire_connected
        self.transport: _FakeTransport | None = None
        self.runtime: _VoicePipelineRuntime | None = None
        self.connected = False
        self.task_manager_ready_at_connect = False
        self.messages: list[dict] = []

    async def connect(self) -> None:
        self.connected = True
        assert self.runtime is not None
        self.runtime._task_manager.get_event_loop()
        self.task_manager_ready_at_connect = True
        if self.fire_connected:
            assert self.transport is not None
            app_message = self.transport.handlers["on_app_message"]
            connected = self.transport.handlers["on_client_connected"]
            await app_message(self.transport, {"type": "client_ready"}, self.pc_id)
            await connected(self.transport, self)

    def send_app_message(self, message: dict) -> None:
        self.messages.append(message)


class _RuntimeHarness:
    def __init__(
        self,
        *,
        fire_connected: bool = False,
        block_welcome: bool = False,
        close_failures: int = 0,
    ) -> None:
        self.connection = _FakeConnection(fire_connected=fire_connected)
        self.transport = _FakeTransport()
        self.worker: _FakeWorker | None = None
        self.runtime: _VoicePipelineRuntime | None = None
        self.block_welcome = block_welcome
        self.close_failures = close_failures

    def build(self, runtime: _VoicePipelineRuntime) -> None:
        self.runtime = runtime
        self.connection.runtime = runtime
        self.connection.transport = self.transport
        worker = _FakeWorker(
            block_welcome=self.block_welcome,
            close_failures=self.close_failures,
        )
        self.worker = worker
        runtime._transport = self.transport  # type: ignore[assignment]
        runtime._worker = worker  # type: ignore[assignment]
        runtime._install_event_handlers(self.transport, worker)  # type: ignore[arg-type]

    async def start(self) -> tuple[asyncio.Task[None], AsyncResourceOwner[_VoicePipelineRuntime]]:
        owner: AsyncResourceOwner[_VoicePipelineRuntime] = AsyncResourceOwner("test runtimes")
        def build(runtime: _VoicePipelineRuntime) -> None:
            self.build(runtime)

        with patch.object(_VoicePipelineRuntime, "_build", build):
            task = asyncio.create_task(
                run_voice_pipeline(cast(Any, self.connection), "session", owner)
            )
            while self.worker is None:
                if task.done():
                    task.result()
                await asyncio.sleep(0)
            await self.worker.run_entered.wait()
        return task, owner


def test_runtime_sets_up_task_manager_before_synchronous_connected_callback():
    async def run() -> None:
        harness = _RuntimeHarness(fire_connected=True)
        task, owner = await harness.start()
        assert harness.worker is not None
        await harness.worker.queue_frame_entered.wait()
        harness.worker.finish_run()
        await task

        assert harness.connection.task_manager_ready_at_connect
        assert harness.runtime is not None and harness.runtime.closed
        assert owner.owned_count == 0
        assert harness.transport.cleanup_calls == 1

    asyncio.run(run())


def test_completed_welcome_task_retires_all_strong_references():
    async def run() -> None:
        harness = _RuntimeHarness(fire_connected=True)
        task, _owner = await harness.start()
        assert harness.worker is not None
        await harness.worker.queue_frame_entered.wait()
        await asyncio.sleep(0)
        assert harness.runtime is not None
        assert harness.runtime._welcome_task is None
        assert harness.runtime._task_manager.current_tasks() == ()
        harness.worker.finish_run()
        await task

    asyncio.run(run())


def test_pipeline_failure_propagates_after_complete_runtime_cleanup():
    async def run() -> None:
        harness = _RuntimeHarness()
        task, owner = await harness.start()
        assert harness.worker is not None
        harness.worker.fail_run(RuntimeError("pipeline exploded"))
        with pytest.raises(RuntimeError, match="pipeline exploded"):
            await task

        assert harness.runtime is not None and harness.runtime.closed
        assert harness.worker.closed
        assert harness.transport.cleanup_calls == 1
        assert owner.owned_count == 0

    asyncio.run(run())


def test_parent_cancellation_waits_for_welcome_physical_cleanup():
    async def run() -> None:
        harness = _RuntimeHarness(fire_connected=True, block_welcome=True)
        task, owner = await harness.start()
        assert harness.worker is not None
        await harness.worker.queue_frame_entered.wait()
        harness.worker.queue_cancel_release.clear()

        task.cancel()
        await harness.worker.queue_cancel_started.wait()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()

        harness.worker.queue_cancel_release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert harness.worker.queue_cancel_finished.is_set()
        assert harness.runtime is not None and harness.runtime.closed
        assert owner.owned_count == 0

    asyncio.run(run())


def test_runtime_cleanup_retries_only_remaining_debt_and_transport_once():
    async def run() -> None:
        harness = _RuntimeHarness(close_failures=1)
        runtime = _VoicePipelineRuntime(cast(Any, harness.connection), "retry")
        harness.build(runtime)

        with pytest.raises(RuntimeError, match="worker cleanup failed"):
            await runtime.aclose()
        assert not runtime.closed
        assert harness.transport.cleanup_calls == 1

        await runtime.aclose()
        assert runtime.closed
        assert harness.worker is not None and harness.worker.close_calls == 2
        assert harness.transport.cleanup_calls == 1

    asyncio.run(run())


def test_shutdown_detaches_handlers_and_late_events_cannot_mutate_runtime():
    async def run() -> None:
        harness = _RuntimeHarness()
        runtime = _VoicePipelineRuntime(cast(Any, harness.connection), "late")
        harness.build(runtime)
        assert harness.worker is not None
        connected = harness.transport.handlers["on_client_connected"]
        app_message = harness.transport.handlers["on_app_message"]

        await runtime.aclose()
        await connected(harness.transport, harness.connection)
        await app_message(harness.transport, {"type": "client_ready"}, harness.connection.pc_id)

        assert not runtime._session_active
        assert not runtime._client_ready.is_set()
        assert harness.worker.queue_calls == 0
        assert harness.transport.handlers == {}

    asyncio.run(run())


class _CleanupProcessor(FrameProcessor):
    def __init__(self, failures: int = 0) -> None:
        super().__init__()
        self.failures = failures
        self.cleanup_calls = 0

    async def cleanup(self) -> None:
        self.cleanup_calls += 1
        if self.cleanup_calls <= self.failures:
            raise RuntimeError("processor cleanup failed")
        await super().cleanup()


def test_retryable_pipeline_retries_only_failed_processor_and_retires_cleanup_task():
    async def run() -> None:
        healthy = _CleanupProcessor()
        flaky = _CleanupProcessor(failures=1)
        pipeline = _RetryableVoicePipeline([healthy, flaky])

        with pytest.raises(RuntimeError, match="processor cleanup failed"):
            await pipeline.cleanup()
        assert pipeline._voice_cleanup_task is None
        assert healthy.cleanup_calls == 1
        assert flaky.cleanup_calls == 1

        await pipeline.cleanup()
        assert pipeline.closed
        assert pipeline._voice_cleanup_task is None
        assert healthy.cleanup_calls == 1
        assert flaky.cleanup_calls == 2

    asyncio.run(run())


def test_task_manager_retires_completed_tasks_immediately():
    async def run() -> None:
        manager = _VoiceTaskManager()
        manager.setup(TaskManagerParams(loop=asyncio.get_running_loop()))

        async def complete() -> None:
            return None

        task = manager.create_task(complete(), "complete")
        await task
        await asyncio.sleep(0)
        assert manager.current_tasks() == ()
        assert manager._tasks == set()

        await manager.aclose()
        assert manager.closed

    asyncio.run(run())


def test_task_manager_backpressures_new_work_until_prior_failure_is_surfaced():
    async def run() -> None:
        manager = _VoiceTaskManager()
        manager.setup(TaskManagerParams(loop=asyncio.get_running_loop()))
        failure = RuntimeError("background task failed")

        async def fail() -> None:
            raise failure

        failed_task = manager.create_task(fail(), "failed")
        while not failed_task.done():
            await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert manager._tasks == {failed_task}

        incoming = asyncio.sleep(3600)
        with pytest.raises(RuntimeError, match="background task failed") as raised:
            manager.create_task(incoming, "must-not-start")

        assert raised.value is failure
        assert getattr(incoming, "cr_frame", None) is None
        assert manager._tasks == set()
        await manager.aclose()

    asyncio.run(run())


def test_task_manager_shutdown_surfaces_failed_task_and_releases_ownership():
    async def run() -> None:
        manager = _VoiceTaskManager()
        manager.setup(TaskManagerParams(loop=asyncio.get_running_loop()))
        failure = RuntimeError("background task failed")

        async def fail() -> None:
            raise failure

        failed_task = manager.create_task(fail(), "failed")
        while not failed_task.done():
            await asyncio.sleep(0)

        with pytest.raises(RuntimeError, match="background task failed") as raised:
            await manager.aclose()

        assert raised.value is failure
        assert manager.closed
        assert manager._tasks == set()
        await manager.aclose()

    asyncio.run(run())


def test_interruption_drain_and_forward_failures_are_both_surfaced():
    async def run() -> None:
        transport = cast(Any, object.__new__(_TaigiSmallWebRTCOutputTransport))
        transport._client = SimpleNamespace(
            _audio_output_track=SimpleNamespace(_chunk_queue=deque()),
        )
        drain_error = RuntimeError("drain failed")
        forward_error = RuntimeError("forward failed")
        forwarded: list[InterruptionFrame] = []

        async def fail_forward(_self, frame) -> None:
            forwarded.append(frame)
            raise forward_error

        with (
            patch("voice.pipeline._drain_chunk_queue", side_effect=drain_error),
            patch.object(SmallWebRTCOutputTransport, "_handle_frame", fail_forward),
            pytest.raises(BaseExceptionGroup) as raised,
        ):
            await transport._handle_frame(InterruptionFrame())

        assert raised.value.exceptions == (drain_error, forward_error)
        assert len(forwarded) == 1

    asyncio.run(run())


class _EventConnection:
    def __init__(self) -> None:
        self._event_handlers: dict[str, SimpleNamespace] = {}

    def event_handler(self, name: str):
        event = self._event_handlers.setdefault(name, SimpleNamespace(handlers=[]))

        def decorator(handler):
            event.handlers.append(handler)
            return handler

        return decorator


def test_transport_dispatch_is_strict_and_detaches_only_its_connection_callbacks():
    async def run() -> None:
        connection = _EventConnection()

        @connection.event_handler("closed")
        async def api_owner_handler(_connection) -> None:
            return None

        transport = _TaigiSmallWebRTCTransport(
            webrtc_connection=connection,  # type: ignore[arg-type]
            params=TransportParams(),
        )

        @transport.event_handler("on_client_connected")
        async def broken(_transport, _connection) -> None:
            raise RuntimeError("event failed")

        with pytest.raises(RuntimeError, match="event failed"):
            await transport._call_event_handler("on_client_connected", connection)

        await transport.cleanup()
        assert transport.closed
        assert transport.connection_handlers_detached
        assert connection._event_handlers["closed"].handlers == [api_owner_handler]
        for name in ("connected", "disconnected", "app-message"):
            assert connection._event_handlers[name].handlers == []

    asyncio.run(run())


def test_transport_constructor_failure_rolls_back_registered_callbacks():
    connection = _EventConnection()

    @connection.event_handler("closed")
    async def api_owner_handler(_connection) -> None:
        return None

    def broken_super(self, webrtc_connection, **_kwargs) -> None:
        self._event_handlers = {}

        @webrtc_connection.event_handler("connected")
        async def leaked(_connection) -> None:
            return None

        raise RuntimeError("constructor failed")

    with (
        patch.object(SmallWebRTCTransport, "__init__", broken_super),
        pytest.raises(RuntimeError, match="constructor failed"),
    ):
        _TaigiSmallWebRTCTransport(
            webrtc_connection=connection,  # type: ignore[arg-type]
            params=TransportParams(),
        )

    assert connection._event_handlers["closed"].handlers == [api_owner_handler]
    assert connection._event_handlers["connected"].handlers == []
