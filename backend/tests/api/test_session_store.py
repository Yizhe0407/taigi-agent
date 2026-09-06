import json
import sqlite3
import time
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest

import api.session_store as session_store_module
from api.session_store import ChatSessionStore, SessionTombstonedError


def _store(tmp_path: Path, **kwargs) -> ChatSessionStore:
    return ChatSessionStore(tmp_path / "sessions.db", **kwargs).open()


def _session_id() -> str:
    return str(uuid4())


class _ConnectionProxy:
    """Inject rollback/close failures while retaining a real sqlite backend."""

    def __init__(
        self,
        target: sqlite3.Connection,
        *,
        rollback_errors: list[BaseException] | None = None,
        close_errors: list[BaseException] | None = None,
    ) -> None:
        self.target = target
        self.rollback_errors = iter(rollback_errors or [])
        self.close_errors = iter(close_errors or [])
        self.rollback_calls = 0
        self.close_calls = 0

    def execute(self, *args, **kwargs):
        return self.target.execute(*args, **kwargs)

    def commit(self) -> None:
        self.target.commit()

    def rollback(self) -> None:
        self.rollback_calls += 1
        error = next(self.rollback_errors, None)
        if error is not None:
            raise error
        self.target.rollback()

    def close(self) -> None:
        self.close_calls += 1
        error = next(self.close_errors, None)
        if error is not None:
            raise error
        self.target.close()


def test_open_failure_retains_exact_connection_until_explicit_close(tmp_path, monkeypatch):
    class BrokenConnection:
        def __init__(self) -> None:
            self.execute_calls = 0
            self.close_calls = 0

        def execute(self, _statement: str):
            self.execute_calls += 1
            if self.execute_calls == 2:
                raise RuntimeError("schema initialization failed")
            return self

        def close(self) -> None:
            self.close_calls += 1

    connection = BrokenConnection()
    monkeypatch.setattr(session_store_module.sqlite3, "connect", lambda *args, **kwargs: connection)
    store = ChatSessionStore(tmp_path / "sessions.db")

    with pytest.raises(RuntimeError, match="schema initialization failed"):
        store.open()

    assert store._connection is connection
    assert not store.opened
    assert connection.close_calls == 0

    store.close()
    assert connection.close_calls == 1
    assert store._connection is None


def test_transaction_and_rollback_failures_are_grouped_and_poison_connection(tmp_path):
    store = _store(tmp_path)
    transaction_error = RuntimeError("transaction failed")
    rollback_error = RuntimeError("rollback failed")
    proxy = _ConnectionProxy(store._conn, rollback_errors=[rollback_error])
    store._connection = cast(sqlite3.Connection, proxy)

    with pytest.raises(BaseExceptionGroup) as raised:
        with store._lock, store._write_transaction():
            raise transaction_error

    assert raised.value.exceptions == (transaction_error, rollback_error)
    assert proxy.rollback_calls == 1
    with pytest.raises(RuntimeError, match="rollback failed") as poisoned:
        store.exists(_session_id())
    assert poisoned.value.__cause__ is raised.value

    # The poisoned connection is still owned solely for deterministic release.
    store.close()
    assert proxy.close_calls == 1
    assert store._connection is None


def test_store_close_failure_retains_connection_for_exactly_one_retry(tmp_path):
    store = _store(tmp_path)
    close_error = RuntimeError("close failed")
    proxy = _ConnectionProxy(store._conn, close_errors=[close_error])
    store._connection = cast(sqlite3.Connection, proxy)

    with pytest.raises(RuntimeError, match="close failed") as raised:
        store.close()
    assert raised.value is close_error
    assert store._connection is proxy
    assert proxy.close_calls == 1

    store.close()
    assert store._connection is None
    assert proxy.close_calls == 2

    store.close()
    assert proxy.close_calls == 2


def test_create_and_load_roundtrip(tmp_path):
    store = _store(tmp_path)
    session_id = _session_id()

    store.create(session_id)
    assert store.load_messages(session_id) == []

    store.save_messages(session_id, [{"role": "user", "content": "你好"}])
    assert store.load_messages(session_id) == [{"role": "user", "content": "你好"}]


def test_duplicate_create_is_an_exact_noop(tmp_path):
    store = _store(tmp_path)
    session_id = _session_id()
    messages = [{"role": "user", "content": "保留我"}]
    store.create(session_id)
    store.save_messages(session_id, messages)
    before = store._conn.execute(
        "SELECT last_used, messages FROM sessions WHERE session_id = ?",
        (session_id,),
    ).fetchone()

    store.create(session_id)

    after = store._conn.execute(
        "SELECT last_used, messages FROM sessions WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    assert after == before
    assert json.loads(after[1]) == messages


def test_load_returns_none_for_unknown_session(tmp_path):
    store = _store(tmp_path)
    assert store.load_messages(_session_id()) is None


def test_load_expires_old_session_row(tmp_path):
    store = _store(tmp_path, ttl_seconds=60)
    session_id = _session_id()
    store.create(session_id)
    store._conn.execute(
        "UPDATE sessions SET last_used = ? WHERE session_id = ?",
        (time.time() - 120, session_id),
    )

    assert store.load_messages(session_id) is None
    assert store.load_messages(session_id) is None


def test_expired_session_can_recover_under_same_client_id(tmp_path):
    store = _store(tmp_path, ttl_seconds=60)
    session_id = _session_id()
    store.create(session_id)
    store.save_messages(session_id, [{"role": "user", "content": "old"}])
    store._conn.execute(
        "UPDATE sessions SET last_used = ? WHERE session_id = ?",
        (time.time() - 120, session_id),
    )

    store.create(session_id)

    assert store.load_messages(session_id) == []


def test_delete_tombstones_session_and_blocks_same_id_recreation(tmp_path):
    store = _store(tmp_path)
    session_id = _session_id()
    store.create(session_id)

    store.delete(session_id)

    assert store.load_messages(session_id) is None
    with pytest.raises(SessionTombstonedError, match=session_id):
        store.create(session_id)


def test_delete_before_late_create_is_durable_across_connections(tmp_path):
    db_path = tmp_path / "sessions.db"
    store_a = ChatSessionStore(db_path).open()
    store_b = ChatSessionStore(db_path).open()
    session_id = _session_id()

    store_a.delete(session_id)

    with pytest.raises(SessionTombstonedError, match=session_id):
        store_b.create(session_id)
    assert store_b.load_messages(session_id) is None


def test_expired_tombstone_allows_same_id_to_be_reused(tmp_path):
    store = _store(tmp_path, ttl_seconds=60)
    session_id = _session_id()
    store.delete(session_id)
    store._conn.execute(
        "UPDATE session_tombstones SET deleted_at = ? WHERE session_id = ?",
        (time.time() - 120, session_id),
    )

    store.create(session_id)

    assert store.load_messages(session_id) == []
    assert (
        store._conn.execute(
            "SELECT 1 FROM session_tombstones WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        is None
    )


def test_messages_persist_across_store_instances(tmp_path):
    db_path = tmp_path / "sessions.db"
    store_a = ChatSessionStore(db_path).open()
    session_id = _session_id()
    store_a.create(session_id)
    store_a.save_messages(session_id, [{"role": "user", "content": "hi"}])
    store_a.close()

    store_b = ChatSessionStore(db_path).open()
    assert store_b.load_messages(session_id) == [{"role": "user", "content": "hi"}]


def test_exists_uses_ttl_without_mutating_row(tmp_path):
    store = _store(tmp_path, ttl_seconds=60)
    live_id = _session_id()
    expired_id = _session_id()
    store.create(live_id)
    store.create(expired_id)
    store._conn.execute(
        "UPDATE sessions SET last_used = ? WHERE session_id = ?",
        (time.time() - 120, expired_id),
    )
    live_before = store._conn.execute(
        "SELECT last_used FROM sessions WHERE session_id = ?",
        (live_id,),
    ).fetchone()[0]

    assert store.exists(live_id) is True
    assert store.exists(expired_id) is False
    assert store.exists(_session_id()) is False

    assert (
        store._conn.execute(
            "SELECT last_used FROM sessions WHERE session_id = ?",
            (live_id,),
        ).fetchone()[0]
        == live_before
    )
    assert (
        store._conn.execute(
            "SELECT 1 FROM sessions WHERE session_id = ?",
            (expired_id,),
        ).fetchone()
        is not None
    )


def test_load_messages_does_not_sweep_unrelated_expired_rows(tmp_path):
    store = _store(tmp_path, ttl_seconds=60)
    expired_id = _session_id()
    live_id = _session_id()
    store.create(expired_id)
    store.create(live_id)
    store._conn.execute(
        "UPDATE sessions SET last_used = ? WHERE session_id = ?",
        (time.time() - 120, expired_id),
    )

    assert store.load_messages(live_id) == []
    assert (
        store._conn.execute(
            "SELECT 1 FROM sessions WHERE session_id = ?",
            (expired_id,),
        ).fetchone()
        is not None
    )
    assert store.load_messages(expired_id) is None


def test_purge_expired_removes_sessions_and_tombstones_in_one_sweep(tmp_path):
    store = _store(tmp_path, ttl_seconds=60)
    expired_session_id = _session_id()
    live_session_id = _session_id()
    tombstoned_id = _session_id()
    store.create(expired_session_id)
    store.create(live_session_id)
    store.delete(tombstoned_id)
    old = time.time() - 120
    store._conn.execute(
        "UPDATE sessions SET last_used = ? WHERE session_id = ?",
        (old, expired_session_id),
    )
    store._conn.execute(
        "UPDATE session_tombstones SET deleted_at = ? WHERE session_id = ?",
        (old, tombstoned_id),
    )

    assert store.purge_expired() == [expired_session_id]
    assert store.session_ids() == {live_session_id}
    assert (
        store._conn.execute(
            "SELECT 1 FROM session_tombstones WHERE session_id = ?",
            (tombstoned_id,),
        ).fetchone()
        is None
    )
