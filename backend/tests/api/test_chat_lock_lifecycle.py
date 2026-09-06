"""Lifecycle tests for chat session lock states and store shutdown.

Two long-uptime resource leaks are covered here:

1. A per-session `asyncio.Lock` outliving its session forever, because the
   session's row was deleted by `load_messages()` (voice reconnect with a
   stale session_id) rather than by `purge_expired()`, so nothing ever
   reported the id to the lock purger.
2. The store's sqlite connection never being closed on lifespan shutdown.

The lock purge must also never remove a Lock that a request is holding or
queued on — doing so lets the next caller build a rival Lock for the same
session, which silently breaks mutual exclusion between two writers.
"""

import asyncio
import threading
import time
from collections.abc import AsyncGenerator
from typing import cast
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

import api
import api.chat as chat
import api.session_store as session_store_module
from agent.session import AgentSession
from api.chat import respond_in_session_stream
from api.session_store import ChatSessionStore
from config import _llm_clients


def _install_store(store: ChatSessionStore) -> chat._ChatStoreRuntime:
    asyncio.run(chat.startup_store(store))
    return chat._require_chat_store_runtime()


def _runtime() -> chat._ChatStoreRuntime:
    return chat._require_chat_store_runtime()


def _backdate(store: ChatSessionStore, session_id: str) -> None:
    """Push a session past its TTL without sleeping."""
    store._conn.execute(
        "UPDATE sessions SET last_used = ? WHERE session_id = ?",
        (time.time() - 10_000, session_id),
    )


async def _collect(session_id: str, message: str) -> str:
    llm_owner = _llm_clients.startup_current_loop()
    try:
        return "".join([chunk async for chunk in respond_in_session_stream(session_id, message)])
    finally:
        await llm_owner.aclose()


def _patch_lifespan_startup(monkeypatch) -> None:
    """Keep lifecycle tests isolated from process-global production owners."""
    monkeypatch.setattr(api, "startup_llm_clients", lambda: object())
    monkeypatch.setattr(api, "close_llm_clients", AsyncMock())
    monkeypatch.setattr(api, "startup_http_client", lambda: None)
    monkeypatch.setattr(api, "startup_departure_streams", AsyncMock())
    monkeypatch.setattr(api, "startup_text_processor", AsyncMock())
    monkeypatch.setattr(api, "startup_store", AsyncMock())
    monkeypatch.setattr(api, "voice_startup", lambda: None)


def test_lock_reclaimed_for_session_deleted_by_expired_load(tmp_path, monkeypatch):
    """An expired authoritative read can delete a row before the sweeper sees it.

    The lock reconciler must therefore diff against live rows rather than rely
    only on ``purge_expired()`` returning every deleted session ID.
    """
    store = ChatSessionStore(tmp_path / "sessions.db")
    runtime = _install_store(store)
    session_id = str(uuid4())
    store.create(session_id)

    async def fake_respond_stream(self, message: str):
        self.messages.append({"role": "user", "content": message})
        yield "ok"

    monkeypatch.setattr(AgentSession, "respond_stream", fake_respond_stream)

    # 1. The session is used once, so its Lock now exists.
    asyncio.run(_collect(session_id, "hello"))
    assert session_id in runtime.session_lock_states

    # 2. The authoritative read observes expiry and deletes the row itself.
    _backdate(store, session_id)
    assert store.load_messages(session_id) is None

    # 3. The row is already gone, so purge_expired() reports nothing — this is
    # precisely why trusting its return value alone leaked the Lock.
    assert store.purge_expired() == []

    # 4. The reconciling purge still has to reclaim the orphaned Lock.
    asyncio.run(chat.purge_expired_locks())
    assert session_id not in runtime.session_lock_states


def test_missing_authoritative_read_retires_lock_on_final_user(tmp_path):
    store = ChatSessionStore(tmp_path / "sessions.db")
    runtime = _install_store(store)
    session_id = str(uuid4())

    with pytest.raises(LookupError, match=session_id):
        asyncio.run(_collect(session_id, "hello"))

    assert session_id not in runtime.session_lock_states


def test_purge_keeps_lock_held_by_an_in_flight_stream(tmp_path, monkeypatch):
    """A Lock held across a live stream must survive purge even when its row is
    already gone — removing it would let the next request create a second Lock
    for the same session and run two writers concurrently."""
    store = ChatSessionStore(tmp_path / "sessions.db")
    runtime = _install_store(store)
    session_id = str(uuid4())
    store.create(session_id)

    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocking_respond_stream(self, message: str):
        entered.set()
        await release.wait()
        yield "ok"

    monkeypatch.setattr(AgentSession, "respond_stream", blocking_respond_stream)

    async def run():
        streaming = asyncio.create_task(_collect(session_id, "hello"))
        await entered.wait()  # stream is inside the lock now

        held = runtime.session_lock_states[session_id]
        assert held.lock.locked()

        # Row disappears mid-stream (explicit DELETE / expiry), so the id/row
        # diff marks this Lock as purgeable.
        await chat.delete_chat_session(UUID(session_id))
        assert runtime.session_lock_states.get(session_id) is held, "DELETE dropped a Lock that a stream was holding"

        release.set()
        await streaming

        # The final holder owns reclamation; no delayed purge pass is needed.
        assert session_id not in runtime.session_lock_states

    asyncio.run(run())


def test_purge_keeps_lock_with_a_queued_waiter(tmp_path):
    """Same protection for a caller that is queued on the Lock but hasn't
    acquired it yet: it registered before awaiting, so the purge sees it."""
    store = ChatSessionStore(tmp_path / "sessions.db")
    runtime = _install_store(store)
    session_id = str(uuid4())
    store.create(session_id)
    store.delete(session_id)  # no live row → the id/row diff wants this Lock gone

    waiter_queued = asyncio.Event()
    waiter_got_lock = asyncio.Event()

    async def waiter():
        waiter_queued.set()
        # Runs straight into the Lock's acquire without another suspension
        # point, so the user count is already bumped once the parent resumes.
        async with chat._hold_session_lock(runtime, session_id):
            waiter_got_lock.set()

    async def run():
        async with chat._hold_session_lock(runtime, session_id):
            held = runtime.session_lock_states[session_id]
            queued = asyncio.create_task(waiter())
            await waiter_queued.wait()
            assert held.users == 2
            assert not waiter_got_lock.is_set()

            await chat.purge_expired_locks()
            assert runtime.session_lock_states.get(session_id) is held, "purge dropped a Lock with a queued waiter"

        await queued
        # The waiter took over the *same* Lock object, never a rival one.
        assert waiter_got_lock.is_set()
        assert session_id not in runtime.session_lock_states

    asyncio.run(run())


def test_lifespan_closes_store_after_the_purge_loop_stops(tmp_path, monkeypatch):
    """Shutdown must close the sqlite connection, and only once the background
    purge loop is done — a query on a closed connection raises."""
    events: list[str] = []

    class RecordingStore(ChatSessionStore):
        def close(self) -> None:
            events.append("store-closed")
            super().close()

    store = RecordingStore(tmp_path / "sessions.db")
    runtime = _install_store(store)

    purge_loop_started = asyncio.Event()

    async def fake_purge_loop():
        purge_loop_started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            events.append("purge-loop-stopped")
            raise

    async def fake_warmup_loop():
        await asyncio.sleep(3600)

    monkeypatch.setattr(api, "run_lock_purge_loop", fake_purge_loop)
    monkeypatch.setattr(api, "_eta_warmup_loop", fake_warmup_loop)
    _patch_lifespan_startup(monkeypatch)
    monkeypatch.setattr(api, "voice_shutdown", AsyncMock())
    monkeypatch.setattr(api, "shutdown_departure_streams", AsyncMock())
    monkeypatch.setattr(api, "shutdown_text_processor", AsyncMock())
    monkeypatch.setattr(api, "aclose_http_client", AsyncMock())

    async def run():
        async with api._lifespan(api.app):
            # Let the loop actually reach its first suspension point, the way a
            # serving process would, so shutdown cancels a *running* task.
            await purge_loop_started.wait()

    asyncio.run(run())

    assert events == ["purge-loop-stopped", "store-closed"]
    with pytest.raises(RuntimeError, match="store is closed"):
        _ = store._conn
    assert runtime.closed
    assert chat._chat_store_runtime is runtime


def test_lifespan_orders_generation_startup_and_dependency_shutdown(monkeypatch):
    events: list[str] = []
    llm_owner = object()

    def start_llm():
        events.append("llm-start")
        return llm_owner

    async def idle_loop():
        await asyncio.Event().wait()

    async def start_departures():
        events.append("departures-start")

    async def start_text_processor():
        events.append("text-start")

    async def start_store():
        events.append("store-start")

    async def close_voice():
        events.append("voice-stop")

    async def close_departures():
        events.append("departures-stop")

    async def close_store():
        events.append("store-stop")

    async def close_llm(owner):
        assert owner is llm_owner
        events.append("llm-stop")

    async def close_text_processor():
        events.append("text-stop")

    async def close_http():
        events.append("http-stop")

    monkeypatch.setattr(api, "startup_llm_clients", start_llm)
    monkeypatch.setattr(api, "startup_http_client", lambda: events.append("http-start"))
    monkeypatch.setattr(api, "startup_departure_streams", start_departures)
    monkeypatch.setattr(api, "startup_text_processor", start_text_processor)
    monkeypatch.setattr(api, "startup_store", start_store)
    monkeypatch.setattr(api, "voice_startup", lambda: events.append("voice-start"))
    monkeypatch.setattr(api, "_eta_warmup_loop", idle_loop)
    monkeypatch.setattr(api, "run_lock_purge_loop", idle_loop)
    monkeypatch.setattr(api, "voice_shutdown", close_voice)
    monkeypatch.setattr(api, "shutdown_departure_streams", close_departures)
    monkeypatch.setattr(api, "close_store", close_store)
    monkeypatch.setattr(api, "close_llm_clients", close_llm)
    monkeypatch.setattr(api, "shutdown_text_processor", close_text_processor)
    monkeypatch.setattr(api, "aclose_http_client", close_http)

    async def run() -> None:
        async with api._lifespan(api.app):
            events.append("serving")

    asyncio.run(run())

    assert events == [
        "llm-start",
        "http-start",
        "departures-start",
        "text-start",
        "store-start",
        "voice-start",
        "serving",
        "voice-stop",
        "departures-stop",
        "store-stop",
        "llm-stop",
        "text-stop",
        "http-stop",
    ]


def test_close_store_is_a_noop_without_a_store():
    assert chat._chat_store_runtime is None
    asyncio.run(chat.close_store())
    assert chat._chat_store_runtime is None


def test_store_open_and_close_run_off_loop_and_waiter_cancellation_joins_threads(tmp_path):
    class BlockingStore(ChatSessionStore):
        def __init__(self, path):
            super().__init__(path)
            self.open_started = threading.Event()
            self.open_release = threading.Event()
            self.close_started = threading.Event()
            self.close_release = threading.Event()
            self.open_thread_id: int | None = None
            self.close_thread_id: int | None = None

        def open(self):
            self.open_thread_id = threading.get_ident()
            self.open_started.set()
            self.open_release.wait()
            return super().open()

        def close(self) -> None:
            self.close_thread_id = threading.get_ident()
            self.close_started.set()
            self.close_release.wait()
            super().close()

    async def wait_for_thread(event: threading.Event) -> None:
        while not event.is_set():
            await asyncio.sleep(0)

    async def run() -> None:
        event_loop_thread_id = threading.get_ident()
        cancelled_open = BlockingStore(tmp_path / "cancelled-open.db")
        opening = asyncio.create_task(chat.startup_store(cancelled_open))
        await wait_for_thread(cancelled_open.open_started)
        opening.cancel()
        await asyncio.sleep(0)
        assert not opening.done(), "startup waiter detached the physical open thread"

        cancelled_open.open_release.set()
        await wait_for_thread(cancelled_open.close_started)
        assert not opening.done(), "startup cancellation skipped transactional rollback"
        cancelled_open.close_release.set()
        with pytest.raises(asyncio.CancelledError):
            await opening

        cancelled_runtime = chat._chat_store_runtime
        assert cancelled_runtime is not None
        assert cancelled_runtime.closed
        assert cancelled_open.open_thread_id != event_loop_thread_id
        assert cancelled_open.close_thread_id != event_loop_thread_id

        live_store = BlockingStore(tmp_path / "cancelled-close.db")
        live_store.open_release.set()
        await chat.startup_store(live_store)
        runtime = chat._require_chat_store_runtime()
        closing = asyncio.create_task(chat.close_store())
        await wait_for_thread(live_store.close_started)
        closing.cancel()
        await asyncio.sleep(0)
        assert not closing.done(), "shutdown waiter detached the physical close thread"

        live_store.close_release.set()
        with pytest.raises(asyncio.CancelledError):
            await closing

        assert runtime.closed
        assert live_store.close_thread_id != event_loop_thread_id

    asyncio.run(run())


def test_sse_disconnect_closes_nested_agent_stream_and_releases_lock(tmp_path, monkeypatch):
    """`async for` does not close a nested async generator on outer `aclose()`.
    The SSE -> session -> agent chain must therefore propagate close explicitly
    so a disconnected client cannot leave the session Lock held until GC."""
    store = ChatSessionStore(tmp_path / "sessions.db")
    runtime = _install_store(store)
    session_uuid = uuid4()
    session_id = str(session_uuid)
    store.create(session_id)
    agent_closed = asyncio.Event()

    async def streaming_reply(self, message: str):
        try:
            yield "第一段"
            await asyncio.Event().wait()
        finally:
            agent_closed.set()

    monkeypatch.setattr(AgentSession, "respond_stream", streaming_reply)

    async def run():
        llm_owner = _llm_clients.startup_current_loop()
        response = await chat.send_chat_message_stream(session_uuid, chat.ChatMessageRequest(message="hello"))
        body_iterator = cast(AsyncGenerator[str, None], response.body_iterator)
        first = await anext(body_iterator)
        assert "第一段" in first
        state = runtime.session_lock_states[session_id]
        assert state.lock.locked()

        await body_iterator.aclose()

        assert agent_closed.is_set()
        assert not state.lock.locked()
        assert state.users == 0
        await llm_owner.aclose()

    asyncio.run(run())


def test_stream_factory_captures_generation_and_never_rebinds_to_successor(tmp_path):
    old_store = ChatSessionStore(tmp_path / "old.db")
    new_store = ChatSessionStore(tmp_path / "new.db")
    session_id = str(uuid4())

    async def run() -> None:
        await chat.startup_store(old_store)
        old_store.create(session_id)
        old_runtime = chat._require_chat_store_runtime()
        stream = respond_in_session_stream(session_id, "late")

        await chat.close_store()
        assert old_runtime.closed
        await chat.startup_store(new_store)
        new_store.create(session_id)
        new_runtime = chat._require_chat_store_runtime()

        with pytest.raises(HTTPException) as raised:
            await anext(stream)
        assert raised.value.status_code == 503
        assert new_runtime.active_operation_count == 0
        assert new_store.load_messages(session_id) == []
        await stream.aclose()

    asyncio.run(run())


def test_active_stream_lease_blocks_shutdown_and_successor_until_release(tmp_path, monkeypatch):
    store = ChatSessionStore(tmp_path / "active.db")
    successor = ChatSessionStore(tmp_path / "successor.db")
    session_id = str(uuid4())
    entered = asyncio.Event()
    agent_closed = asyncio.Event()

    async def blocking_stream(self, message: str):
        del self, message
        try:
            entered.set()
            yield "first"
            await asyncio.Event().wait()
        finally:
            agent_closed.set()

    monkeypatch.setattr(AgentSession, "respond_stream", blocking_stream)

    async def run() -> None:
        llm_owner = _llm_clients.startup_current_loop()
        await chat.startup_store(store)
        store.create(session_id)
        runtime = chat._require_chat_store_runtime()
        stream = respond_in_session_stream(session_id, "hello")
        assert await anext(stream) == "first"
        await entered.wait()
        assert runtime.active_operation_count == 1

        shutdown = asyncio.create_task(chat.close_store())
        while not runtime.closing:
            await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not shutdown.done()

        with pytest.raises(RuntimeError, match="before shutdown succeeds"):
            await chat.startup_store(successor)
        assert not successor.opened

        await stream.aclose()
        assert agent_closed.is_set()
        await shutdown
        assert runtime.closed

        await chat.startup_store(successor)
        assert successor.opened
        await llm_owner.aclose()

    asyncio.run(run())


def test_live_generation_rejects_replacement_until_explicit_shutdown(tmp_path):
    first = ChatSessionStore(tmp_path / "first.db")
    second = ChatSessionStore(tmp_path / "second.db")

    async def run() -> None:
        await chat.startup_store(first)
        first_runtime = chat._require_chat_store_runtime()

        with pytest.raises(RuntimeError, match="before shutdown succeeds"):
            await chat.startup_store(second)

        assert chat._chat_store_runtime is first_runtime
        assert first.opened
        assert not second.opened
        assert second._connection is None

        await chat.close_store()
        assert first_runtime.closed
        await chat.startup_store(second)
        second_runtime = chat._require_chat_store_runtime()
        assert second_runtime is not first_runtime
        assert second.opened

    asyncio.run(run())


def test_startup_retains_failed_open_owner_until_same_connection_closes(tmp_path, monkeypatch):
    schema_error = RuntimeError("schema initialization failed")
    close_error = RuntimeError("close failed")
    real_connect = session_store_module.sqlite3.connect
    connect_calls = 0

    class BrokenConnection:
        def __init__(self) -> None:
            self.execute_calls = 0
            self.close_calls = 0

        def execute(self, _statement: str):
            self.execute_calls += 1
            if self.execute_calls == 2:
                raise schema_error
            return self

        def close(self) -> None:
            self.close_calls += 1
            if self.close_calls == 1:
                raise close_error

    broken_connection = BrokenConnection()

    def connect(*args, **kwargs):
        nonlocal connect_calls
        connect_calls += 1
        if connect_calls == 1:
            return broken_connection
        return real_connect(*args, **kwargs)

    monkeypatch.setenv("CHAT_SESSION_DB", str(tmp_path / "failed.db"))
    monkeypatch.setattr(session_store_module.sqlite3, "connect", connect)

    async def run() -> None:
        with pytest.raises(BaseExceptionGroup) as raised:
            await chat.startup_store()

        assert raised.value.exceptions == (schema_error, close_error)
        failed_runtime = chat._chat_store_runtime
        assert failed_runtime is not None
        assert failed_runtime.store._connection is broken_connection
        assert failed_runtime.closing
        assert not failed_runtime.closed
        assert broken_connection.close_calls == 1

        successor = ChatSessionStore(tmp_path / "successor.db")
        with pytest.raises(RuntimeError, match="before shutdown succeeds"):
            await chat.startup_store(successor)
        assert not successor.opened
        assert successor._connection is None

        await chat.close_store()
        assert broken_connection.close_calls == 2
        assert failed_runtime.closed
        assert chat._chat_store_runtime is failed_runtime

        await chat.startup_store(successor)
        assert chat._chat_store_runtime is not failed_runtime
        assert successor.opened

    asyncio.run(run())


def test_failed_close_retains_owner_locks_and_blocks_successor_until_retry(tmp_path):
    class RetryableCloseStore(ChatSessionStore):
        def __init__(self, path):
            super().__init__(path)
            self.close_attempts = 0

        def close(self) -> None:
            self.close_attempts += 1
            if self.close_attempts == 1:
                raise RuntimeError("close failed")
            super().close()

    store = RetryableCloseStore(tmp_path / "sessions.db")
    second = ChatSessionStore(tmp_path / "second.db")

    async def run() -> None:
        await chat.startup_store(store)
        runtime = chat._require_chat_store_runtime()
        lock_state = chat._SessionLockState()
        runtime.session_lock_states["existing"] = lock_state

        with pytest.raises(RuntimeError, match="close failed"):
            await chat.close_store()

        assert chat._chat_store_runtime is runtime
        assert runtime.closing
        assert not runtime.closed
        assert runtime.session_lock_states == {"existing": lock_state}
        with pytest.raises(RuntimeError, match="before shutdown succeeds"):
            await chat.startup_store(second)
        assert not second.opened

        await chat.close_store()

        assert store.close_attempts == 2
        assert runtime.closed
        assert runtime.session_lock_states == {}
        assert chat._chat_store_runtime is runtime

        await chat.startup_store(second)
        assert second.opened

    asyncio.run(run())


def test_lifespan_continues_cleanup_after_component_error(monkeypatch):
    events: list[str] = []

    async def idle_loop():
        await asyncio.sleep(3600)

    async def broken_voice_shutdown():
        events.append("voice")
        raise RuntimeError("voice cleanup failed")

    async def close_llm(_owner):
        events.append("llm")

    async def close_departures():
        events.append("departures")

    async def close_store():
        events.append("store")

    async def close_text_processor():
        events.append("text")

    async def close_http():
        events.append("http")

    monkeypatch.setattr(api, "_eta_warmup_loop", idle_loop)
    monkeypatch.setattr(api, "run_lock_purge_loop", idle_loop)
    _patch_lifespan_startup(monkeypatch)
    monkeypatch.setattr(api, "voice_shutdown", broken_voice_shutdown)
    monkeypatch.setattr(api, "shutdown_departure_streams", close_departures)
    monkeypatch.setattr(api, "close_store", close_store)
    monkeypatch.setattr(api, "close_llm_clients", close_llm)
    monkeypatch.setattr(api, "shutdown_text_processor", close_text_processor)
    monkeypatch.setattr(api, "aclose_http_client", close_http)

    async def run():
        with pytest.raises(RuntimeError, match="voice cleanup failed"):
            async with api._lifespan(api.app):
                pass

    asyncio.run(run())
    assert events == ["voice", "departures", "store", "llm", "text", "http"]


def test_api_shutdown_retry_only_retries_remaining_cleanup_debt(monkeypatch):
    events: list[str] = []
    voice_attempts = 0

    async def close_voice():
        nonlocal voice_attempts
        voice_attempts += 1
        events.append(f"voice-{voice_attempts}")
        if voice_attempts == 1:
            raise RuntimeError("voice cleanup failed")

    async def record(label: str) -> None:
        events.append(label)

    monkeypatch.setattr(api, "voice_shutdown", close_voice)
    monkeypatch.setattr(api, "shutdown_departure_streams", lambda: record("departures"))
    monkeypatch.setattr(api, "close_store", lambda: record("store"))
    monkeypatch.setattr(api, "close_llm_clients", lambda _owner: record("llm"))
    monkeypatch.setattr(api, "shutdown_text_processor", lambda: record("text"))
    monkeypatch.setattr(api, "aclose_http_client", lambda: record("http"))

    async def run() -> None:
        resources = api._ApiLifespanResources(
            voice_pending=True,
            departures_pending=True,
            store_pending=True,
            llm_owner=cast(api._LlmClientLifecycleOwner, object()),
            llm_pending=True,
            text_processor_pending=True,
            http_pending=True,
        )
        with pytest.raises(RuntimeError, match="voice cleanup failed"):
            await api._join_api_shutdown(resources)

        assert not resources.closed
        assert events == ["voice-1", "departures", "store", "llm", "text", "http"]

        await api._join_api_shutdown(resources)
        assert resources.closed
        assert events == [
            "voice-1",
            "departures",
            "store",
            "llm",
            "text",
            "http",
            "voice-2",
        ]

    asyncio.run(run())


def test_lifespan_cancellation_waits_for_complete_ordered_cleanup(monkeypatch):
    events: list[str] = []
    voice_started = asyncio.Event()
    release_voice = asyncio.Event()

    async def idle_loop():
        await asyncio.sleep(3600)

    async def close_voice():
        events.append("voice-start")
        voice_started.set()
        await release_voice.wait()
        events.append("voice-done")

    async def close_llm(_owner):
        events.append("llm")

    async def close_departures():
        events.append("departures")

    async def close_store():
        events.append("store")

    async def close_text_processor():
        events.append("text")

    async def close_http():
        events.append("http")

    monkeypatch.setattr(api, "_eta_warmup_loop", idle_loop)
    monkeypatch.setattr(api, "run_lock_purge_loop", idle_loop)
    _patch_lifespan_startup(monkeypatch)
    monkeypatch.setattr(api, "voice_shutdown", close_voice)
    monkeypatch.setattr(api, "shutdown_departure_streams", close_departures)
    monkeypatch.setattr(api, "close_store", close_store)
    monkeypatch.setattr(api, "close_llm_clients", close_llm)
    monkeypatch.setattr(api, "shutdown_text_processor", close_text_processor)
    monkeypatch.setattr(api, "aclose_http_client", close_http)

    async def run():
        lifespan = api._lifespan(api.app)
        await lifespan.__aenter__()
        exiting = asyncio.create_task(lifespan.__aexit__(None, None, None))
        await voice_started.wait()

        exiting.cancel()
        await asyncio.sleep(0)
        assert not exiting.done(), "waiter cancellation interrupted physical cleanup"

        release_voice.set()
        with pytest.raises(asyncio.CancelledError):
            await exiting

    asyncio.run(run())
    assert events == [
        "voice-start",
        "voice-done",
        "departures",
        "store",
        "llm",
        "text",
        "http",
    ]


def test_lifespan_rolls_back_voice_and_first_task_when_second_task_creation_fails(monkeypatch):
    events: list[str] = []
    created_tasks: list[asyncio.Task[None]] = []
    real_create_task = asyncio.create_task

    async def idle_loop():
        await asyncio.sleep(3600)

    def create_task(coroutine, *, name=None):
        if name == "chat-lock-purge-loop":
            raise RuntimeError("task creation failed")
        task = real_create_task(coroutine, name=name)
        if name == "eta-warmup-loop":
            created_tasks.append(task)
        return task

    async def close_voice():
        events.append("voice")

    async def close_llm(_owner):
        events.append("llm")

    async def close_departures():
        events.append("departures")

    async def close_store():
        events.append("store")

    async def close_text_processor():
        events.append("text")

    async def close_http():
        events.append("http")

    monkeypatch.setattr(api, "_eta_warmup_loop", idle_loop)
    monkeypatch.setattr(api, "run_lock_purge_loop", idle_loop)
    _patch_lifespan_startup(monkeypatch)
    monkeypatch.setattr(api, "voice_startup", lambda: events.append("startup"))
    monkeypatch.setattr(api, "voice_shutdown", close_voice)
    monkeypatch.setattr(api, "shutdown_departure_streams", close_departures)
    monkeypatch.setattr(api, "close_store", close_store)
    monkeypatch.setattr(api, "close_llm_clients", close_llm)
    monkeypatch.setattr(api, "shutdown_text_processor", close_text_processor)
    monkeypatch.setattr(api, "aclose_http_client", close_http)
    monkeypatch.setattr(api.asyncio, "create_task", create_task)

    async def run():
        lifespan = api._lifespan(api.app)
        with pytest.raises(RuntimeError, match="task creation failed"):
            await lifespan.__aenter__()

    asyncio.run(run())

    assert len(created_tasks) == 1
    assert created_tasks[0].done()
    assert created_tasks[0].cancelled()
    assert events == ["startup", "voice", "departures", "store", "llm", "text", "http"]


def test_lifespan_aggregates_startup_and_rollback_failures(monkeypatch):
    events: list[str] = []
    real_create_task = asyncio.create_task

    async def idle_loop():
        await asyncio.sleep(3600)

    def create_task(coroutine, *, name=None):
        if name == "chat-lock-purge-loop":
            raise RuntimeError("task creation failed")
        return real_create_task(coroutine, name=name)

    async def broken_voice_shutdown():
        events.append("voice")
        raise RuntimeError("voice cleanup failed")

    async def close_llm(_owner):
        events.append("llm")

    async def close_departures():
        events.append("departures")

    async def close_store():
        events.append("store")

    async def close_text_processor():
        events.append("text")

    async def close_http():
        events.append("http")

    monkeypatch.setattr(api, "_eta_warmup_loop", idle_loop)
    monkeypatch.setattr(api, "run_lock_purge_loop", idle_loop)
    _patch_lifespan_startup(monkeypatch)
    monkeypatch.setattr(api, "voice_startup", lambda: events.append("startup"))
    monkeypatch.setattr(api, "voice_shutdown", broken_voice_shutdown)
    monkeypatch.setattr(api, "shutdown_departure_streams", close_departures)
    monkeypatch.setattr(api, "close_store", close_store)
    monkeypatch.setattr(api, "close_llm_clients", close_llm)
    monkeypatch.setattr(api, "shutdown_text_processor", close_text_processor)
    monkeypatch.setattr(api, "aclose_http_client", close_http)
    monkeypatch.setattr(api.asyncio, "create_task", create_task)

    async def run():
        lifespan = api._lifespan(api.app)
        with pytest.raises(BaseExceptionGroup) as captured:
            await lifespan.__aenter__()
        messages = [str(error) for error in captured.value.exceptions]
        assert messages == ["task creation failed", "voice cleanup failed"]

    asyncio.run(run())
    assert events == ["startup", "voice", "departures", "store", "llm", "text", "http"]


def test_failed_llm_cleanup_stays_on_app_state_and_blocks_silent_replacement(monkeypatch):
    first_owner = object()
    second_owner = object()
    owners = iter([first_owner, second_owner])
    close_attempts: list[object] = []

    async def idle_loop() -> None:
        await asyncio.Event().wait()

    def start_llm() -> object:
        return next(owners)

    async def close_llm(owner: object) -> None:
        close_attempts.append(owner)
        if close_attempts == [first_owner]:
            raise RuntimeError("llm cleanup failed")

    monkeypatch.setattr(api, "startup_llm_clients", start_llm)
    monkeypatch.setattr(api, "close_llm_clients", close_llm)
    monkeypatch.setattr(api, "startup_http_client", lambda: None)
    monkeypatch.setattr(api, "startup_departure_streams", AsyncMock())
    monkeypatch.setattr(api, "startup_text_processor", AsyncMock())
    monkeypatch.setattr(api, "startup_store", AsyncMock())
    monkeypatch.setattr(api, "voice_startup", lambda: None)
    monkeypatch.setattr(api, "_eta_warmup_loop", idle_loop)
    monkeypatch.setattr(api, "run_lock_purge_loop", idle_loop)
    monkeypatch.setattr(api, "voice_shutdown", AsyncMock())
    monkeypatch.setattr(api, "shutdown_departure_streams", AsyncMock())
    monkeypatch.setattr(api, "close_store", AsyncMock())
    monkeypatch.setattr(api, "shutdown_text_processor", AsyncMock())
    monkeypatch.setattr(api, "aclose_http_client", AsyncMock())

    async def run() -> None:
        with pytest.raises(RuntimeError, match="llm cleanup failed"):
            async with api._lifespan(api.app):
                assert api.app.state.llm_client_owner is first_owner

        assert api.app.state.llm_client_owner is first_owner

        async with api._lifespan(api.app):
            assert api.app.state.llm_client_owner is second_owner

        assert not hasattr(api.app.state, "llm_client_owner")

    asyncio.run(run())
    assert close_attempts == [first_owner, first_owner, second_owner]
