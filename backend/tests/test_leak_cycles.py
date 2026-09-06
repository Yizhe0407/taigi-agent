"""Repeated-cycle leak regression: nothing may survive its own teardown.

Ownership counters prove bookkeeping; weak references prove the physical
objects were actually released. Both are asserted after N cycles so a
per-cycle retention (task, traceback, lock, HTTP response, SSE lease) fails
here instead of only showing up as production RSS growth.
"""

from __future__ import annotations

import asyncio
import gc
import logging
import weakref
from collections.abc import Awaitable
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch
from uuid import uuid4

import pytest

import api.chat as chat
import api.departures
import api.voice as voice_module
from agent.llm_client import call_llm_stream
from agent.session import AgentSession
from api.departures import shutdown_departure_streams, startup_departure_streams
from api.session_store import ChatSessionStore
from async_lifecycle import AsyncResourceOwner
from telemetry import AgentTelemetry

CYCLES = 25


def _survivors(refs: list[weakref.ref]) -> list[Any]:
    gc.collect()
    return [alive for ref in refs if (alive := ref()) is not None]


class _NoOpTelemetry:
    @contextmanager
    def start_span(self, name, attributes=None):
        yield SimpleNamespace(name=name)

    def set_content(self, span, key, text, *, limit=None):
        pass

    def mark_span_error(self, span, *, error_type, exception=None, description=None):
        pass

    def record_llm_duration(self, duration_s, *, model, operation, outcome):
        pass

    def record_llm_retry(self, *, operation, error_type):
        pass


class _CyclePeer:
    def __init__(self, pc_id: str) -> None:
        self.pc_id = pc_id
        self.closed = False
        self.payload = bytearray(4096)
        self._event_handlers: dict[str, SimpleNamespace] = {}

    def construct(self) -> None:
        pass

    def event_handler(self, name: str):
        event = self._event_handlers.setdefault(name, SimpleNamespace(handlers=[]))

        def decorator(handler):
            event.handlers.append(handler)
            return handler

        return decorator

    async def initialize(self, *, sdp: str, type: str) -> None:
        del sdp, type

    def get_answer(self) -> dict[str, str]:
        return {"sdp": "answer", "type": "answer", "pc_id": self.pc_id}

    async def disconnect(self) -> None:
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


def _offer_request() -> Any:
    return SimpleNamespace(pc_id=None, sdp="offer", type="offer", restart_pc=False)


def test_repeated_offers_release_every_peer_connection():
    async def run() -> list[weakref.ref]:
        handler = voice_module._VoiceConnectionHandler(ice_servers=[])
        refs: list[weakref.ref] = []

        for index in range(CYCLES):
            peer = _CyclePeer(f"pc-{index}")
            refs.append(weakref.ref(peer))
            with patch.object(handler, "_allocate_connection", return_value=cast(Any, peer)):
                async def callback(_connection: Any) -> None:
                    return None

                await handler.handle_web_request(_offer_request(), callback)
            await handler.release_connection(cast(Any, peer))
            del peer

        assert handler.owned_count == 0
        assert handler.pending_count == 0
        assert handler.failed_count == 0
        assert handler._pcs_map == {}
        assert handler._connection_entries == {}
        await handler.close()
        return refs

    assert _survivors(asyncio.run(run())) == []


def test_repeated_pipeline_crashes_release_their_frames():
    async def run() -> tuple[list[weakref.ref], int]:
        owner = voice_module._PipelineTaskOwner()
        refs: list[weakref.ref] = []

        for index in range(CYCLES):
            peer = _CyclePeer(f"pc-{index}")
            refs.append(weakref.ref(peer))

            async def crashing_pipeline(connection: Any = peer) -> None:
                # The payload is part of the failure: a retained exception pins
                # its args just as surely as its traceback pins frames.
                raise RuntimeError("pipeline crashed", connection)

            await owner.start(crashing_pipeline(), name=f"voice-pipeline-{index}")
            del peer, crashing_pipeline

        for _ in range(CYCLES * 10):
            if owner.owned_count == 0:
                break
            await asyncio.sleep(0)

        assert owner.owned_count == 0
        failures = owner.failure_count
        retained = _survivors(refs)
        with pytest.raises(BaseException):
            await owner.aclose()
        assert owner.failure_count == 0
        return retained, failures

    # pytest's log capture retains exc_info tracebacks for the whole test;
    # production handlers format and drop. Emit no records at all instead.
    logging.disable(logging.CRITICAL)
    try:
        retained, failures = asyncio.run(run())
    finally:
        logging.disable(logging.NOTSET)

    assert failures == voice_module._MAX_RETAINED_PIPELINE_FAILURES
    assert retained == []


def test_aggregated_pipeline_failures_release_their_frames():
    """Independent teardown errors reach the caller only through the group."""

    async def run() -> list[Any]:
        owner = voice_module._PipelineTaskOwner()
        refs: list[weakref.ref] = []

        for index in range(CYCLES):
            peer = _CyclePeer(f"pc-{index}")
            refs.append(weakref.ref(peer))

            async def aggregated_failure(connection: Any = peer) -> None:
                async def close_peer() -> None:
                    assert connection is not None
                    raise RuntimeError("disconnect failed")

                errors: list[BaseException] = []
                for _ in range(2):
                    try:
                        await close_peer()
                    except BaseException as error:  # noqa: BLE001
                        errors.append(error)
                raise BaseExceptionGroup("teardown failed", errors)

            await owner.start(aggregated_failure(), name=f"voice-pipeline-{index}")
            del peer, aggregated_failure

        for _ in range(CYCLES * 10):
            if owner.owned_count == 0:
                break
            await asyncio.sleep(0)

        assert owner.owned_count == 0
        retained = _survivors(refs)
        with pytest.raises(BaseException):
            await owner.aclose()
        return retained

    logging.disable(logging.CRITICAL)
    try:
        retained = asyncio.run(run())
    finally:
        logging.disable(logging.NOTSET)

    assert retained == []


def test_repeated_chat_turns_retire_locks_and_agent_sessions(tmp_path, monkeypatch):
    from config import _llm_clients

    session_refs: list[weakref.ref] = []
    real_rehydrate = chat._rehydrate_session

    def tracking_rehydrate(*args, **kwargs):
        session = real_rehydrate(*args, **kwargs)
        session_refs.append(weakref.ref(session))
        return session

    async def fake_respond_stream(self, message: str):
        del message
        yield "ok"

    monkeypatch.setattr(chat, "_rehydrate_session", tracking_rehydrate)
    monkeypatch.setattr(AgentSession, "respond_stream", fake_respond_stream)

    store = ChatSessionStore(tmp_path / "sessions.db")

    async def run() -> chat._ChatStoreRuntime:
        _llm_owner = _llm_clients.startup_current_loop()
        await chat.startup_store(store)
        runtime = chat._require_chat_store_runtime()

        for _ in range(CYCLES):
            session_id = str(uuid4())
            store.create(session_id)
            stream = chat.respond_in_session_stream(session_id, "hello")
            assert "".join([chunk async for chunk in stream]) == "ok"
            del stream
            store.delete(session_id)
            chat._retire_session_lock(runtime, session_id)

        assert runtime.session_lock_states == {}
        assert runtime.active_operation_count == 0
        await chat.close_store()
        await _llm_owner.aclose()
        chat._chat_store_runtime = None
        return runtime

    runtime = asyncio.run(run())
    assert runtime.session_lock_states == {}
    assert _survivors(session_refs) == []


class _CycleStream:
    def __init__(self) -> None:
        self.payload = bytearray(4096)
        self.closed = False
        self._chunks = iter([
            SimpleNamespace(
                choices=[SimpleNamespace(delta=SimpleNamespace(content="hi", tool_calls=None))]
            )
        ])

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._chunks)
        except StopIteration:
            raise StopAsyncIteration from None

    async def close(self) -> None:
        self.closed = True


def test_repeated_llm_streams_close_and_release_every_response():
    async def run() -> list[weakref.ref]:
        owner: AsyncResourceOwner[Any] = AsyncResourceOwner("LLM HTTP requests")
        telemetry = cast(AgentTelemetry, _NoOpTelemetry())
        refs: list[weakref.ref] = []

        for _ in range(CYCLES):
            stream = _CycleStream()
            refs.append(weakref.ref(stream))
            client = SimpleNamespace(
                chat=SimpleNamespace(
                    completions=SimpleNamespace(create=_returning(stream))
                )
            )

            events = call_llm_stream(
                client,
                "test-model",
                [{"role": "user", "content": "hi"}],
                None,
                {},
                telemetry,
                http_owner=owner,
                operation="respond",
            )
            async for _kind, _value in events:
                pass
            assert stream.closed
            del stream, client, events

        assert owner.owned_count == 0
        assert owner.pending_count == 0
        assert owner.failed_count == 0
        await owner.aclose()
        return refs

    assert _survivors(asyncio.run(run())) == []


def _returning(stream: Any):
    async def create(**_kwargs: Any) -> Any:
        return stream

    return create


def test_repeated_sse_streams_release_every_lease(monkeypatch):
    from tests.api.test_api import _departure_snapshot

    async def fake_snapshot(*, updated_at=None):
        return _departure_snapshot()

    monkeypatch.setattr(api.departures, "get_departure_snapshot_here", fake_snapshot)

    async def run() -> api.departures._DepartureStreamRuntime:
        await startup_departure_streams()
        runtime = api.departures._departure_runtime
        assert runtime is not None

        for _ in range(CYCLES):
            response = await api.departures.stream_departures_here()
            await anext(response.body_iterator)
            await response.body_iterator.aclose()
            del response

        assert runtime.active_count == 0
        await shutdown_departure_streams()
        return runtime

    runtime = asyncio.run(run())
    assert runtime.active_count == 0
    assert runtime.closed


def test_repeated_lifespans_leave_no_llm_client_state(monkeypatch):
    from config import _LlmClientCache

    class _Client:
        def __init__(self) -> None:
            self.payload = bytearray(4096)
            self._closed = False

        def is_closed(self) -> bool:
            return self._closed

        async def close(self) -> None:
            self._closed = True

    refs: list[weakref.ref] = []

    def build(*_args: Any) -> Any:
        client = _Client()
        refs.append(weakref.ref(client))
        return client

    monkeypatch.setattr("config._build_llm_client", build)
    cache = _LlmClientCache(maxsize=4)

    for _ in range(CYCLES):
        async def clean_lifespan() -> None:
            _lifecycle_owner = cache.startup_current_loop()
            cache.get_session_resources("http://llm.local/v1", "test")
            await cache.aclose_current_loop()

        asyncio.run(clean_lifespan())

    assert cache._owners == {}
    assert _survivors(refs) == []
