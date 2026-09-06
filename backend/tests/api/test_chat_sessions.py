"""Client-owned chat session lifecycle contract."""

import asyncio
import threading
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

import api.chat as chat
from api import app
from api.session_store import ChatSessionStore


def _client(tmp_path, monkeypatch) -> tuple[TestClient, ChatSessionStore]:
    store = ChatSessionStore(tmp_path / "sessions.db")
    asyncio.run(chat.startup_store(store))
    monkeypatch.setattr(chat, "get_settings", lambda: object())
    return TestClient(app), store


def test_put_is_idempotent_and_preserves_existing_messages(tmp_path, monkeypatch):
    client, store = _client(tmp_path, monkeypatch)
    session_id = str(uuid4())

    first = client.put(f"/api/chat/sessions/{session_id}")
    assert first.status_code == 200
    assert first.json() == {"sessionId": session_id}

    messages = [{"role": "user", "content": "勿清掉"}]
    store.save_messages(session_id, messages)
    second = client.put(f"/api/chat/sessions/{session_id}")

    assert second.status_code == 200
    assert second.json() == {"sessionId": session_id}
    assert store.load_messages(session_id) == messages


def test_delete_before_delayed_put_prevents_session_resurrection(tmp_path, monkeypatch):
    client, store = _client(tmp_path, monkeypatch)
    session_id = str(uuid4())

    deleted = client.delete(f"/api/chat/sessions/{session_id}")
    delayed_create = client.put(f"/api/chat/sessions/{session_id}")

    assert deleted.status_code == 204
    assert delayed_create.status_code == 409
    assert store.load_messages(session_id) is None


def test_delete_keeps_sqlite_off_loop_and_lock_registry_on_loop(tmp_path, monkeypatch):
    store = ChatSessionStore(tmp_path / "sessions.db")
    asyncio.run(chat.startup_store(store))
    runtime = chat._require_chat_store_runtime()
    session_id = uuid4()
    store.create(str(session_id))
    threads: dict[str, int] = {}

    original_delete = store.delete

    def recording_delete(session_key: str) -> None:
        threads["sqlite"] = threading.get_ident()
        original_delete(session_key)

    def recording_retire(owner: chat._ChatStoreRuntime, session_key: str) -> None:
        assert owner is runtime
        asyncio.get_running_loop()
        threads["lock_registry"] = threading.get_ident()
        runtime.session_lock_states.pop(session_key, None)

    monkeypatch.setattr(store, "delete", recording_delete)
    monkeypatch.setattr(chat, "_retire_session_lock", recording_retire)

    async def run() -> None:
        threads["event_loop"] = threading.get_ident()
        await chat.delete_chat_session(UUID(str(session_id)))

    asyncio.run(run())

    assert threads["sqlite"] != threads["event_loop"]
    assert threads["lock_registry"] == threads["event_loop"]


def test_legacy_server_generated_post_route_is_removed(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)

    response = client.post("/api/chat/sessions")

    assert response.status_code in {404, 405}
    assert "/api/chat/sessions" not in app.openapi()["paths"]
