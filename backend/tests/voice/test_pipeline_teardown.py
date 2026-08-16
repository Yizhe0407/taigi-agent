"""Teardown/leak regression tests for run_voice_pipeline().

These exercise the real SmallWebRTCConnection / SmallWebRTCRequestHandler pair
(a genuine aiortc peer connection built from a locally generated SDP offer, ICE
servers disabled so gathering finishes instantly) and stub only the pipeline
internals. The assertions are therefore about observable state — is the peer
connection still held by pipecat's _pcs_map, is the pc closed, did the pending
welcome task actually get cancelled — not about mocks having been called.
"""

import asyncio
import contextlib
from unittest.mock import MagicMock, patch

import pytest
from aiortc import RTCConfiguration, RTCPeerConnection
from pipecat.transports.smallwebrtc.request_handler import SmallWebRTCRequest, SmallWebRTCRequestHandler

from voice.pipeline import run_voice_pipeline

# ---------------------------------------------------------------------------
# Real-connection fixtures
# ---------------------------------------------------------------------------


async def _make_real_connection():
    """Build a real SmallWebRTCConnection registered in a real request handler.

    Returns (handler, connection, browser_side_pc). The browser side never
    completes ICE — we only need a well-formed offer so pipecat creates a real
    pc; the answer and close paths then behave exactly as in production.
    """
    browser = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    browser.createDataChannel("chat")
    browser.addTransceiver("audio", direction="sendrecv")
    offer = await browser.createOffer()
    await browser.setLocalDescription(offer)

    handler = SmallWebRTCRequestHandler(ice_servers=[])
    request = SmallWebRTCRequest(sdp=browser.localDescription.sdp, type=browser.localDescription.type)

    captured = {}

    async def _capture(connection):
        captured["connection"] = connection

    answer = await handler.handle_web_request(request, _capture)
    assert answer is not None
    return handler, captured["connection"], browser


async def _settle(handler: SmallWebRTCRequestHandler, timeout: float = 3.0) -> None:
    """Wait for pipecat's async "closed" handler to evict the pc from _pcs_map.

    aiortc emits connectionstatechange through pyee's AsyncIOEventEmitter, so
    the pop runs in a task scheduled by close(), not inline.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while handler._pcs_map and loop.time() < deadline:
        await asyncio.sleep(0.01)


class _FakeTransport:
    """Stand-in for _TaigiSmallWebRTCTransport: records the event handlers so a
    test can fire on_client_connected / on_app_message / on_client_disconnected."""

    def __init__(self, *_args, **_kwargs):
        self.handlers: dict = {}

    def event_handler(self, name: str):
        def decorator(fn):
            self.handlers[name] = fn
            return fn

        return decorator

    def input(self):
        return MagicMock()

    def output(self):
        return MagicMock()


class _FakeWorker:
    """Stand-in for PipelineWorker with a controllable run()/queue_frame()."""

    def __init__(self, *_args, **_kwargs):
        self.run_entered = asyncio.Event()
        self.queue_frame_entered = asyncio.Event()
        self.queue_frame_cancelled = False
        self.cancelled = False
        self._run_gate: asyncio.Future = asyncio.get_running_loop().create_future()
        self._queue_gate = asyncio.Event()

    async def run(self, *_args, **_kwargs):
        self.run_entered.set()
        await self._run_gate

    def fail_run(self, exc: BaseException) -> None:
        if not self._run_gate.done():
            self._run_gate.set_exception(exc)

    def finish_run(self) -> None:
        if not self._run_gate.done():
            self._run_gate.set_result(None)

    async def cancel(self):
        self.cancelled = True
        self.finish_run()

    async def queue_frame(self, _frame):
        self.queue_frame_entered.set()
        try:
            await self._queue_gate.wait()
        except asyncio.CancelledError:
            self.queue_frame_cancelled = True
            raise


class _Harness:
    """Holds the fake transport/worker created inside run_voice_pipeline."""

    def __init__(self):
        self.transport: _FakeTransport | None = None
        self.worker: _FakeWorker | None = None

    async def started(self, timeout: float = 3.0) -> _FakeWorker:
        """Wait until run_voice_pipeline reached `await task.run(...)`."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while self.worker is None and loop.time() < deadline:
            await asyncio.sleep(0)
        assert self.worker is not None, "pipeline never created its PipelineWorker"
        await asyncio.wait_for(self.worker.run_entered.wait(), timeout=timeout)
        return self.worker


@contextlib.contextmanager
def _stubbed_pipeline():
    """Replace every heavyweight pipeline part; keep the WebRTC connection real."""
    harness = _Harness()

    def _make_transport(*args, **kwargs):
        harness.transport = _FakeTransport(*args, **kwargs)
        return harness.transport

    def _make_worker(*args, **kwargs):
        harness.worker = _FakeWorker(*args, **kwargs)
        return harness.worker

    with contextlib.ExitStack() as stack:
        for name in (
            "VADProcessor",
            "SileroVADAnalyzer",
            "BargeInProcessor",
            "BreezeSTTService",
            "TaigiBusAgentProcessor",
            "TaigiTTSService",
            "SubtitleSyncProcessor",
            "Pipeline",
            "get_telemetry",
        ):
            stack.enter_context(patch(f"voice.pipeline.{name}", MagicMock()))
        stack.enter_context(patch("voice.pipeline._TaigiSmallWebRTCTransport", _make_transport))
        stack.enter_context(patch("voice.pipeline.PipelineWorker", _make_worker))
        yield harness


# ---------------------------------------------------------------------------
# The finally block must release the peer connection
# ---------------------------------------------------------------------------


def test_pipeline_crash_releases_peer_connection_from_handler():
    """When task.run() raises, nothing else closes the pc — pipecat only evicts
    it from _pcs_map on the pc's own "closed" event, and only pc.close() emits
    that. Without the finally-block close, the pc leaks for the process' life."""

    async def run():
        handler, connection, browser = await _make_real_connection()
        try:
            assert connection.pc_id in handler._pcs_map

            with _stubbed_pipeline() as harness:
                pipeline = asyncio.create_task(run_voice_pipeline(connection, "sess-crash"))
                worker = await harness.started()
                worker.fail_run(RuntimeError("pipeline exploded"))
                with pytest.raises(RuntimeError, match="pipeline exploded"):
                    await pipeline

            await _settle(handler)
            assert handler._pcs_map == {}, "peer connection still held by pipecat after crash"
            assert connection.pc.connectionState == "closed"
        finally:
            await browser.close()

    asyncio.run(run())


def test_pipeline_crash_preserves_original_exception_over_teardown_failure():
    """A failure while closing the connection must not mask the real error."""

    async def run():
        handler, connection, browser = await _make_real_connection()
        try:
            with _stubbed_pipeline() as harness:

                async def _boom():
                    raise OSError("close failed")

                with patch.object(connection, "disconnect", _boom):
                    pipeline = asyncio.create_task(run_voice_pipeline(connection, "sess-mask"))
                    worker = await harness.started()
                    worker.fail_run(RuntimeError("pipeline exploded"))
                    # The pipeline's own error surfaces, not the teardown OSError.
                    with pytest.raises(RuntimeError, match="pipeline exploded"):
                        await pipeline
        finally:
            await connection.disconnect()
            await _settle(handler)
            await browser.close()

    asyncio.run(run())


def test_pipeline_teardown_cancels_pending_welcome_task():
    """The fire-and-forget welcome task must not outlive the pipeline: park it
    inside queue_frame, tear the pipeline down, and it must come back cancelled."""

    async def run():
        handler, connection, browser = await _make_real_connection()
        try:
            with _stubbed_pipeline() as harness:
                pipeline = asyncio.create_task(run_voice_pipeline(connection, "sess-welcome"))
                worker = await harness.started()

                # client_ready first, so _send_welcome skips its 3 s wait and
                # runs straight into queue_frame(), where our fake parks it.
                await harness.transport.handlers["on_app_message"](None, '{"type": "client_ready"}', None)
                await harness.transport.handlers["on_client_connected"](None, None)
                await asyncio.wait_for(worker.queue_frame_entered.wait(), timeout=3.0)

                worker.fail_run(RuntimeError("pipeline exploded"))
                with pytest.raises(RuntimeError, match="pipeline exploded"):
                    await pipeline

                assert worker.queue_frame_cancelled, "welcome task was left running after teardown"

            await _settle(handler)
            assert handler._pcs_map == {}
        finally:
            await browser.close()

    asyncio.run(run())


def test_normal_disconnect_path_still_closes_and_returns_cleanly():
    """The usual client-disconnect path is unchanged: on_client_disconnected
    cancels the worker, run_voice_pipeline returns without raising, and the pc
    still ends up closed and unreferenced (disconnect() is idempotent)."""

    async def run():
        handler, connection, browser = await _make_real_connection()
        try:
            with _stubbed_pipeline() as harness:
                pipeline = asyncio.create_task(run_voice_pipeline(connection, "sess-normal"))
                worker = await harness.started()
                await harness.transport.handlers["on_client_disconnected"](None, None)
                await asyncio.wait_for(pipeline, timeout=3.0)
                assert worker.cancelled

            await _settle(handler)
            assert handler._pcs_map == {}
            assert connection.pc.connectionState == "closed"
        finally:
            await browser.close()

    asyncio.run(run())
