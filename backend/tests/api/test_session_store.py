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
