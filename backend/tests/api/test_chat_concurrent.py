"""Tests for respond_in_session_stream concurrent serialization."""

import asyncio

from api.chat import respond_in_session_stream, set_store
from api.session_store import ChatSessionStore


async def _collect(session_id: str, message: str) -> str:
    return "".join([chunk async for chunk in respond_in_session_stream(session_id, message)])


def test_concurrent_respond_no_lost_update(tmp_path, monkeypatch):
    """Two concurrent calls on the same session must not overwrite each other."""
    store = ChatSessionStore(tmp_path / "sessions.db")
    set_store(store)
    session_id = store.create()

    from agent.session import AgentSession

    async def slow_respond_stream(self, message: str):
        await asyncio.sleep(0.05)  # yield so both coroutines start before either saves
        self.messages.append({"role": "user", "content": message})
        yield f"reply to {message}"

    monkeypatch.setattr(AgentSession, "respond_stream", slow_respond_stream)

    async def run():
        await asyncio.gather(
            _collect(session_id, "hello"),
            _collect(session_id, "world"),
        )

    asyncio.run(run())

    saved = store.load_messages(session_id)
    user_texts = [m["content"] for m in saved if m["role"] == "user"]
    assert "hello" in user_texts, f"'hello' missing from {user_texts}"
    assert "world" in user_texts, f"'world' missing from {user_texts}"
