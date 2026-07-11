"""SSE chat endpoint：delta 事件、done 收尾、404 與串流中錯誤。"""

import json

from fastapi.testclient import TestClient

from agent.session import AgentSession
from api import app
from api.chat import set_store
from api.session_store import ChatSessionStore


def _sse_payloads(text: str) -> list[dict]:
    return [json.loads(line[len("data: ") :]) for line in text.split("\n\n") if line.startswith("data: ")]


def _make_session(tmp_path) -> tuple[TestClient, str]:
    store = ChatSessionStore(tmp_path / "sessions.db")
    set_store(store)
    return TestClient(app), store.create()


def test_stream_endpoint_emits_deltas_then_done(tmp_path, monkeypatch):
    client, session_id = _make_session(tmp_path)

    async def fake_stream(self, message: str):
        yield "第一句。"
        yield "第二句。"

    monkeypatch.setattr(AgentSession, "respond_stream", fake_stream)

    response = client.post(f"/api/chat/sessions/{session_id}/messages/stream", json={"message": "hi"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert _sse_payloads(response.text) == [
        {"delta": "第一句。"},
        {"delta": "第二句。"},
        {"done": True},
    ]


def test_stream_endpoint_missing_session_is_404(tmp_path):
    client, _ = _make_session(tmp_path)

    response = client.post("/api/chat/sessions/nope/messages/stream", json={"message": "hi"})

    assert response.status_code == 404


def test_stream_endpoint_midstream_error_becomes_error_event(tmp_path, monkeypatch):
    client, session_id = _make_session(tmp_path)

    async def broken_stream(self, message: str):
        yield "講到一半"
        raise RuntimeError("boom")

    monkeypatch.setattr(AgentSession, "respond_stream", broken_stream)

    response = client.post(f"/api/chat/sessions/{session_id}/messages/stream", json={"message": "hi"})

    payloads = _sse_payloads(response.text)
    assert payloads[0] == {"delta": "講到一半"}
    assert "error" in payloads[-1]
