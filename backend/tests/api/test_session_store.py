import time
from pathlib import Path

from api.session_store import ChatSessionStore


def _store(tmp_path: Path, **kwargs) -> ChatSessionStore:
    return ChatSessionStore(tmp_path / "sessions.db", **kwargs)


def test_create_and_load_roundtrip(tmp_path):
    store = _store(tmp_path)

    session_id = store.create()
    assert store.load_messages(session_id) == []

    store.save_messages(session_id, [{"role": "user", "content": "你好"}])
    assert store.load_messages(session_id) == [{"role": "user", "content": "你好"}]


def test_load_returns_none_for_unknown_session(tmp_path):
    store = _store(tmp_path)
    assert store.load_messages("missing") is None


def test_load_expires_old_sessions(tmp_path):
    store = _store(tmp_path, ttl_seconds=0.01)
    session_id = store.create()
    time.sleep(0.05)
    # Past TTL — load both surfaces None and deletes the row.
    assert store.load_messages(session_id) is None
    assert store.load_messages(session_id) is None


def test_delete_removes_session(tmp_path):
    store = _store(tmp_path)
    session_id = store.create()
    store.delete(session_id)
    assert store.load_messages(session_id) is None


def test_messages_persist_across_store_instances(tmp_path):
    db_path = tmp_path / "sessions.db"
    store_a = ChatSessionStore(db_path)
    session_id = store_a.create()
    store_a.save_messages(session_id, [{"role": "user", "content": "hi"}])
    store_a.close()

    store_b = ChatSessionStore(db_path)
    assert store_b.load_messages(session_id) == [{"role": "user", "content": "hi"}]


def test_exists_true_for_live_session(tmp_path):
    store = _store(tmp_path)
    session_id = store.create()
    assert store.exists(session_id) is True


def test_exists_false_for_unknown_session(tmp_path):
    store = _store(tmp_path)
    assert store.exists("missing") is False


def test_exists_false_for_expired_session(tmp_path):
    store = _store(tmp_path, ttl_seconds=0.01)
    session_id = store.create()
    time.sleep(0.05)
    # Past TTL — same "gone" verdict as load_messages, but exists() must not
    # delete the row itself (it's a read-only pre-check).
    assert store.exists(session_id) is False


def test_exists_does_not_bump_last_used_or_delete_row(tmp_path):
    store = _store(tmp_path, ttl_seconds=0.05)
    session_id = store.create()
    created_last_used = store._conn.execute("SELECT last_used FROM sessions WHERE session_id = ?", (session_id,)).fetchone()[0]

    time.sleep(0.02)
    assert store.exists(session_id) is True

    row = store._conn.execute("SELECT last_used FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
    assert row is not None  # exists() must not delete even an expired row
    assert row[0] == created_last_used  # and must not bump last_used either


def test_load_messages_no_longer_eagerly_purges_other_expired_sessions(tmp_path):
    """Regression guard for the perf fix: load_messages() must not run a
    full-table purge_expired() sweep on every call anymore — that's now the
    background loop's job (api.chat.run_lock_purge_loop, every 300s).

    A sibling session past TTL should still have its row in the table right
    after an unrelated load_messages() call, even though it *would* have
    been deleted by the old eager `self.purge_expired()` at the top of
    load_messages().
    """
    store = _store(tmp_path, ttl_seconds=0.01)
    expired_id = store.create()
    time.sleep(0.05)  # expired_id now past TTL

    live_id = store.create()  # fresh last_used, well within TTL
    store.load_messages(live_id)  # unrelated read — must not sweep expired_id's row

    row = store._conn.execute("SELECT 1 FROM sessions WHERE session_id = ?", (expired_id,)).fetchone()
    assert row is not None  # still present — only a background purge_expired() removes it

    # The store's own TTL semantics for the expired session are unchanged:
    # asking for it directly still reports it as gone (and cleans it up then).
    assert store.load_messages(expired_id) is None
