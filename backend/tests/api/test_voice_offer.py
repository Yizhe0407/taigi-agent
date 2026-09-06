"""Transactional WebRTC offer and connection ownership coverage."""

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import nullcontext
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

import api.voice as voice_module
from async_lifecycle import AsyncResourceOwner


@pytest.fixture(autouse=True)
def _fresh_voice_runtime():
    asyncio.run(voice_module.shutdown())
    voice_module.startup()
    yield
    asyncio.run(voice_module.shutdown())


class _FakeConnection:
    def __init__(
        self,
        pc_id: str = "pc-test",
        *,
        initialize_error: BaseException | None = None,
        construct_error: BaseException | None = None,
        disconnect_error: BaseException | None = None,
        registration_error: BaseException | None = None,
    ) -> None:
        self.pc_id = pc_id
        self.initialize_error = initialize_error
        self.construct_error = construct_error
        self.disconnect_error = disconnect_error
        self.registration_error = registration_error
        self.initialize_started = asyncio.Event()
        self.initialize_release = asyncio.Event()
        self.initialize_release.set()
        self.disconnect_started = asyncio.Event()
        self.disconnect_release = asyncio.Event()
        self.disconnect_release.set()
        self.disconnect_calls = 0
        self.closed = False
        self._event_handlers: dict[str, SimpleNamespace] = {}

    def construct(self) -> None:
        if self.construct_error is not None:
            raise self.construct_error

    def event_handler(self, name: str):
        if self.registration_error is not None:
            raise self.registration_error
        event = self._event_handlers.setdefault(name, SimpleNamespace(handlers=[]))

        def decorator(handler):
            event.handlers.append(handler)
            return handler

        return decorator

    async def initialize(self, *, sdp: str, type: str) -> None:
        del sdp, type
        self.initialize_started.set()
        await self.initialize_release.wait()
        if self.initialize_error is not None:
            raise self.initialize_error

    async def renegotiate(self, **_kwargs: Any) -> None:
        return None

    def get_answer(self) -> dict[str, str]:
        return {"sdp": "answer", "type": "answer", "pc_id": self.pc_id}

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        self.disconnect_started.set()
        await self.disconnect_release.wait()
        if self.disconnect_error is not None:
            raise self.disconnect_error
        if not self.closed:
            await self.emit_closed()

    async def emit_closed(self) -> None:
        if self.closed:
            return
        self.closed = True
        event = self._event_handlers.get("closed")
        if event is None:
            return
        for handler in tuple(event.handlers):
            result = handler(self)
            if isinstance(result, Awaitable):
                await result


def _request() -> Any:
    return SimpleNamespace(
        pc_id=None,
        sdp="offer",
        type="offer",
        restart_pc=False,
    )


def _install_connection(
    handler: voice_module._VoiceConnectionHandler,
    connection: _FakeConnection,
):
    return patch.object(handler, "_allocate_connection", return_value=cast(Any, connection))


def test_partial_transport_construction_failure_rolls_back_owned_connection():
    async def run() -> None:
        handler = voice_module._VoiceConnectionHandler(ice_servers=[])
        connection = _FakeConnection(construct_error=RuntimeError("construct failed"))

        with _install_connection(handler, connection):
            with pytest.raises(RuntimeError, match="construct failed"):
                await handler.handle_web_request(_request(), AsyncMock())

        assert connection.disconnect_calls == 1
        assert handler.owned_count == 0
        assert handler.pending_count == 0
        assert handler._pcs_map == {}
        assert handler._connection_entries == {}

    asyncio.run(run())


def test_partial_initialize_failure_rolls_back_physical_connection():
    async def run() -> None:
        handler = voice_module._VoiceConnectionHandler(ice_servers=[])
        connection = _FakeConnection(initialize_error=RuntimeError("initialize failed"))

        with _install_connection(handler, connection):
            with pytest.raises(RuntimeError, match="initialize failed"):
                await handler.handle_web_request(_request(), AsyncMock())

        assert connection.disconnect_calls == 1
        assert handler.owned_count == 0
        assert handler.pending_count == 0
        assert handler._pcs_map == {}
        assert handler._connection_entries == {}

    asyncio.run(run())


def test_callback_registration_failure_rolls_back_unpublished_connection():
    async def run() -> None:
        handler = voice_module._VoiceConnectionHandler(ice_servers=[])
        connection = _FakeConnection(registration_error=RuntimeError("registration failed"))

        with _install_connection(handler, connection):
            with pytest.raises(RuntimeError, match="registration failed"):
                await handler.handle_web_request(_request(), AsyncMock())

        assert connection.disconnect_calls == 1
        assert handler.owned_count == 0
        assert handler._pcs_map == {}

    asyncio.run(run())


def test_publication_failure_rolls_back_adopted_connection():
    async def run() -> None:
        handler = voice_module._VoiceConnectionHandler(ice_servers=[])
        connection = _FakeConnection()

        with (
            _install_connection(handler, connection),
            patch.object(handler, "_publish_connection", side_effect=RuntimeError("publish failed")),
        ):
            with pytest.raises(RuntimeError, match="publish failed"):
                await handler.handle_web_request(_request(), AsyncMock())

        assert connection.disconnect_calls == 1
        assert handler.owned_count == 0
        assert handler.pending_count == 0
        assert handler._pcs_map == {}

    asyncio.run(run())


def test_http_404_remains_primary_when_disconnect_fails_then_cleanup_retries():
    async def run() -> None:
        cleanup_error = OSError("disconnect failed")
        handler = voice_module._VoiceConnectionHandler(ice_servers=[])
        connection = _FakeConnection(disconnect_error=cleanup_error)

        async def missing_session(_connection: Any) -> None:
            raise HTTPException(status_code=404, detail="missing")

        with _install_connection(handler, connection):
            with pytest.raises(HTTPException) as excinfo:
                await handler.handle_web_request(_request(), missing_session)

        assert excinfo.value.status_code == 404
        assert excinfo.value.__cause__ is cleanup_error
        assert handler.failed_count == 1
        assert handler.owned_count == 1

        connection.disconnect_error = None
        await handler.close()
        assert connection.disconnect_calls == 2
        assert handler.owned_count == 0
        assert handler._pcs_map == {}

    asyncio.run(run())


def test_spontaneous_close_immediately_retires_authoritative_owner_entry():
    async def run() -> None:
        handler = voice_module._VoiceConnectionHandler(ice_servers=[])
        connection = _FakeConnection()

        with _install_connection(handler, connection):
            await handler.handle_web_request(_request(), AsyncMock())

        assert handler.owned_count == 1
        assert handler._pcs_map == {connection.pc_id: connection}
        await connection.emit_closed()
        assert handler.owned_count == 0
        assert handler._pcs_map == {}
        assert handler._connection_entries == {}

        await handler.close()
        assert connection.disconnect_calls == 0

    asyncio.run(run())


def test_spontaneous_close_and_concurrent_release_share_one_disconnect():
    async def run() -> None:
        handler = voice_module._VoiceConnectionHandler(ice_servers=[])
        connection = _FakeConnection()
        connection.disconnect_release.clear()

        with _install_connection(handler, connection):
            await handler.handle_web_request(_request(), AsyncMock())

        endpoint_release = asyncio.create_task(handler.release_connection(cast(Any, connection)))
        await connection.disconnect_started.wait()
        owner_close = asyncio.create_task(handler.close())
        await asyncio.sleep(0)

        await connection.emit_closed()
        connection.disconnect_release.set()
        await endpoint_release
        await owner_close

        assert connection.disconnect_calls == 1
        assert handler.owned_count == 0
        assert handler._pcs_map == {}

    asyncio.run(run())


def test_shutdown_gate_waits_for_inflight_initialize_then_prevents_publication():
    async def run() -> None:
        handler = voice_module._VoiceConnectionHandler(ice_servers=[])
        connection = _FakeConnection()
        connection.initialize_release.clear()

        with _install_connection(handler, connection):
            offer = asyncio.create_task(handler.handle_web_request(_request(), AsyncMock()))
            await connection.initialize_started.wait()
            close = asyncio.create_task(handler.close())
            await asyncio.sleep(0)
            assert not close.done()

            connection.initialize_release.set()
            with pytest.raises(RuntimeError, match="owner is closed"):
                await offer
            await close

        assert connection.disconnect_calls == 1
        assert handler.pending_count == 0
        assert handler.owned_count == 0
        assert handler._pcs_map == {}

    asyncio.run(run())


def test_completed_pipeline_task_is_reported_then_immediately_retired():
    async def run() -> None:
        owner = voice_module._PipelineTaskOwner()

        async def complete() -> None:
            return None

        task = await owner.start(complete(), name="quick-pipeline")
        await task
        await asyncio.sleep(0)
        assert owner.owned_count == 0

        await owner.aclose()
        assert owner.closed

    asyncio.run(run())


def test_failed_pipeline_task_releases_its_frames_and_surfaces_once():
    async def run() -> None:
        owner = voice_module._PipelineTaskOwner()
        failure = RuntimeError("pipeline crashed")

        async def fail() -> None:
            raise failure

        task = await owner.start(fail(), name="failed-pipeline")
        while not task.done():
            await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert owner.owned_count == 0
        assert owner.failure_count == 1

        with pytest.raises(RuntimeError, match="RuntimeError: pipeline crashed") as raised:
            await owner.aclose()

        assert raised.value is not failure
        assert owner.failure_count == 0
        assert owner.closed

        await owner.aclose()

    asyncio.run(run())


def test_offer_uses_exact_session_and_new_runtime_owner_contract():
    async def run() -> None:
        session_id = str(uuid4())
        connection = _FakeConnection()
        seen: list[tuple[str, AsyncResourceOwner[Any]]] = []
        started = asyncio.Event()
        release = asyncio.Event()
        released_connections: list[Any] = []

        class Store:
            def load_messages(self, resolved: str) -> list[dict]:
                assert resolved == session_id
                return []

        class Handler:
            async def handle_web_request(
                self,
                _request: Any,
                callback: Callable[[Any], Awaitable[None]],
            ) -> dict[str, str]:
                await callback(connection)
                return connection.get_answer()

            async def release_connection(self, released: Any) -> None:
                released_connections.append(released)

        async def fake_pipeline(
            passed_connection: Any,
            resolved_session_id: str,
            runtime_owner: AsyncResourceOwner[Any],
        ) -> None:
            assert passed_connection is connection
            seen.append((resolved_session_id, runtime_owner))
            started.set()
            await release.wait()

        runtime = voice_module._require_voice_runtime()
        with (
            patch.object(runtime, "handler", Handler()),
            patch.object(voice_module, "_configured_ice_servers", AsyncMock()),
            patch.object(
                voice_module,
                "chat_store_operation",
                return_value=nullcontext(SimpleNamespace(store=Store())),
            ),
            patch("voice.pipeline.run_voice_pipeline", fake_pipeline),
        ):
            answer = await voice_module.webrtc_offer({"sdp": "offer", "type": "offer", "session_id": session_id})
            await started.wait()
            assert answer["pc_id"] == connection.pc_id
            assert seen == [(session_id, runtime.pipeline_runtimes)]
            assert runtime.pipeline_tasks.owned_count == 1

            release.set()
            for _ in range(100):
                if runtime.pipeline_tasks.owned_count == 0:
                    break
                await asyncio.sleep(0)
            assert runtime.pipeline_tasks.owned_count == 0
            assert released_connections == [connection]

    asyncio.run(run())


def test_offer_requires_session_id_before_acquiring_turn_credentials():
    async def run() -> None:
        configured = AsyncMock()
        runtime = voice_module._require_voice_runtime()
        with patch.object(voice_module, "_configured_ice_servers", configured):
            with pytest.raises(HTTPException) as excinfo:
                await voice_module.webrtc_offer({"sdp": "offer", "type": "offer"})
        assert excinfo.value.status_code == 422
        configured.assert_not_awaited()
        assert runtime.pipeline_tasks.owned_count == 0

    asyncio.run(run())
