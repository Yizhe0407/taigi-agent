"""SSE chat endpoint：delta 事件、done 收尾、404 與串流中錯誤。"""

import json
import time
from collections.abc import Iterator
from contextlib import contextmanager
from uuid import uuid4

from fastapi.testclient import TestClient

import api.chat as chat
from agent.session import AgentSession
from api import app
from api.session_store import ChatSessionStore


def _sse_payloads(text: str) -> list[dict]:
    return [json.loads(line[len("data: ") :]) for line in text.split("\n\n") if line.startswith("data: ")]


@contextmanager
def _make_session(
    tmp_path,
    monkeypatch,
    *,
    ttl_seconds: float = 3600,
) -> Iterator[tuple[TestClient, str]]:
    store = ChatSessionStore(tmp_path / "sessions.db", ttl_seconds=ttl_seconds)
    session_id = str(uuid4())

    async def start_store() -> None:
        await chat.startup_store(store)

    monkeypatch.setattr("api.startup_store", start_store)
    with TestClient(app) as client:
        store.create(session_id)
        yield client, session_id


def test_stream_endpoint_emits_deltas_then_done(tmp_path, monkeypatch):
    async def fake_stream(self, message: str):
        yield "第一句。"
        yield "第二句。"

    monkeypatch.setattr(AgentSession, "respond_stream", fake_stream)

    with _make_session(tmp_path, monkeypatch) as (client, session_id):
        response = client.post(f"/api/chat/sessions/{session_id}/messages/stream", json={"message": "hi"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert _sse_payloads(response.text) == [
        {"delta": "第一句。"},
        {"delta": "第二句。"},
        {"done": True},
    ]


def test_stream_endpoint_missing_session_is_404(tmp_path, monkeypatch):
    with _make_session(tmp_path, monkeypatch) as (client, _session_id):
        response = client.post(f"/api/chat/sessions/{uuid4()}/messages/stream", json={"message": "hi"})

    assert response.status_code == 404


def test_stream_endpoint_expired_session_is_404(tmp_path, monkeypatch):
    """The pre-stream check keeps the same TTL verdict through exists()."""
    with _make_session(tmp_path, monkeypatch, ttl_seconds=0.01) as (client, session_id):
        time.sleep(0.05)
        response = client.post(f"/api/chat/sessions/{session_id}/messages/stream", json={"message": "hi"})

    assert response.status_code == 404


def test_stream_endpoint_midstream_error_becomes_error_event(tmp_path, monkeypatch):
    async def broken_stream(self, message: str):
        yield "講到一半"
        raise RuntimeError("boom")

    monkeypatch.setattr(AgentSession, "respond_stream", broken_stream)

    with _make_session(tmp_path, monkeypatch) as (client, session_id):
        response = client.post(f"/api/chat/sessions/{session_id}/messages/stream", json={"message": "hi"})

    payloads = _sse_payloads(response.text)
    assert payloads[0] == {"delta": "講到一半"}
    assert "error" in payloads[-1]
