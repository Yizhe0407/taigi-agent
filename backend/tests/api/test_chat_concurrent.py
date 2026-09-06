"""Tests for respond_in_session_stream concurrent serialization."""

import asyncio
from uuid import uuid4

import api.chat as chat
from api.chat import respond_in_session_stream
from api.session_store import ChatSessionStore
from config import _llm_clients


async def _collect(session_id: str, message: str) -> str:
    return "".join([chunk async for chunk in respond_in_session_stream(session_id, message)])


def test_concurrent_respond_no_lost_update(tmp_path, monkeypatch):
    """Two concurrent calls on the same session must not overwrite each other."""
    store = ChatSessionStore(tmp_path / "sessions.db")
    session_id = str(uuid4())

    from agent.session import AgentSession

    async def slow_respond_stream(self, message: str):
        await asyncio.sleep(0.05)  # yield so both coroutines start before either saves
        self.messages.append({"role": "user", "content": message})
        yield f"reply to {message}"

    monkeypatch.setattr(AgentSession, "respond_stream", slow_respond_stream)

    async def run() -> list[dict]:
        llm_owner = _llm_clients.startup_current_loop()
        await chat.startup_store(store)
        try:
            await asyncio.to_thread(store.create, session_id)
            await asyncio.gather(
                _collect(session_id, "hello"),
                _collect(session_id, "world"),
            )
            saved = await asyncio.to_thread(store.load_messages, session_id)
            assert saved is not None
            return saved
        finally:
            await chat.close_store()
            await llm_owner.aclose()

    saved = asyncio.run(run())
    user_texts = [m["content"] for m in saved if m["role"] == "user"]
    assert "hello" in user_texts, f"'hello' missing from {user_texts}"
    assert "world" in user_texts, f"'world' missing from {user_texts}"
