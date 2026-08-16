"""Lifecycle tests for `api.chat._session_locks` and store shutdown.

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
import sqlite3
import time
from unittest.mock import AsyncMock

import pytest

import api
import api.chat as chat
from agent.session import AgentSession
from api.chat import respond_in_session_stream, set_store
from api.session_store import ChatSessionStore


@pytest.fixture(autouse=True)
def _reset_chat_globals():
    """Keep the module-level store / lock dicts from leaking between tests."""
    yield
    chat._session_locks.clear()
    chat._session_lock_users.clear()
    if chat._store is not None:
        chat._store.close()
        chat._store = None


def _backdate(store: ChatSessionStore, session_id: str) -> None:
    """Push a session past its TTL without sleeping."""
    store._conn.execute(
        "UPDATE sessions SET last_used = ? WHERE session_id = ?",
        (time.time() - 10_000, session_id),
    )


async def _collect(session_id: str, message: str) -> str:
    return "".join([chunk async for chunk in respond_in_session_stream(session_id, message)])


def test_lock_reclaimed_for_session_deleted_by_expired_load(tmp_path, monkeypatch):
    """The exact kiosk leak sequence: used session → expires → voice reconnect
    validates the stale id via `load_messages` (which deletes the row in place
    and reports nothing) → front-end moves to a new id. `purge_expired()` can
    never name the dead id, so the purge has to diff against live rows.
    """
    store = ChatSessionStore(tmp_path / "sessions.db")
    set_store(store)
    session_id = store.create()

    async def fake_respond_stream(self, message: str):
        self.messages.append({"role": "user", "content": message})
        yield "ok"

    monkeypatch.setattr(AgentSession, "respond_stream", fake_respond_stream)

    # 1. The session is used once, so its Lock now exists.
    asyncio.run(_collect(session_id, "hello"))
    assert session_id in chat._session_locks

    # 2-4. It expires; a voice reconnect carrying the stale id calls
    # load_messages, which deletes the row itself and returns None. Voice then
    # falls back to a brand new session, so the old id is never seen again.
    _backdate(store, session_id)
    assert store.load_messages(session_id) is None
    new_session_id = store.create()

    # 5. The row is already gone, so purge_expired() reports nothing — this is
    # precisely why trusting its return value alone leaked the Lock.
    assert store.purge_expired() == []

    # 6. The reconciling purge still has to reclaim the orphaned Lock.
    asyncio.run(chat.purge_expired_locks())
    assert session_id not in chat._session_locks
    assert session_id not in chat._session_lock_users
    # The live session's own Lock (if any) must survive the same pass.
    assert set(chat._session_locks) <= {new_session_id}


def test_purge_keeps_lock_held_by_an_in_flight_stream(tmp_path, monkeypatch):
    """A Lock held across a live stream must survive purge even when its row is
    already gone — removing it would let the next request create a second Lock
    for the same session and run two writers concurrently."""
    store = ChatSessionStore(tmp_path / "sessions.db")
    set_store(store)
    session_id = store.create()

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

        held = chat._session_locks[session_id]
        assert held.locked()

        # Row disappears mid-stream (explicit DELETE / expiry), so the id/row
        # diff marks this Lock as purgeable.
        store.delete(session_id)
        await chat.purge_expired_locks()
        assert chat._session_locks.get(session_id) is held, "purge dropped a Lock that a stream was holding"

        release.set()
        await streaming

        # Once nobody holds it, the next pass reclaims it — no permanent entry.
        await chat.purge_expired_locks()
        assert session_id not in chat._session_locks

    asyncio.run(run())


def test_purge_keeps_lock_with_a_queued_waiter(tmp_path):
    """Same protection for a caller that is queued on the Lock but hasn't
    acquired it yet: it registered before awaiting, so the purge sees it."""
    store = ChatSessionStore(tmp_path / "sessions.db")
    set_store(store)
    session_id = store.create()
    store.delete(session_id)  # no live row → the id/row diff wants this Lock gone

    waiter_queued = asyncio.Event()
    waiter_got_lock = asyncio.Event()

    async def waiter():
        waiter_queued.set()
        # Runs straight into the Lock's acquire without another suspension
        # point, so the user count is already bumped once the parent resumes.
        async with chat._hold_session_lock(session_id):
            waiter_got_lock.set()

    async def run():
        async with chat._hold_session_lock(session_id):
            held = chat._session_locks[session_id]
            queued = asyncio.create_task(waiter())
            await waiter_queued.wait()
            assert chat._session_lock_users[session_id] == 2
            assert not waiter_got_lock.is_set()

            await chat.purge_expired_locks()
            assert chat._session_locks.get(session_id) is held, "purge dropped a Lock with a queued waiter"

        await queued
        # The waiter took over the *same* Lock object, never a rival one.
        assert waiter_got_lock.is_set()
        assert session_id not in chat._session_lock_users

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
    set_store(store)

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
    monkeypatch.setattr(api, "voice_shutdown", AsyncMock())
    monkeypatch.setattr(api, "aclose_http_client", AsyncMock())
    monkeypatch.setattr(api, "get_provider", lambda: object())

    async def run():
        async with api._lifespan(api.app):
            # Let the loop actually reach its first suspension point, the way a
            # serving process would, so shutdown cancels a *running* task.
            await purge_loop_started.wait()

    asyncio.run(run())

    assert events == ["purge-loop-stopped", "store-closed"]
    with pytest.raises(sqlite3.ProgrammingError):
        store._conn.execute("SELECT 1")
    assert chat._store is None


def test_close_store_is_a_noop_without_a_store():
    chat._store = None
    chat.close_store()  # must not lazily create a sqlite file just to close it
    assert chat._store is None
