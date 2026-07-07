"""Tests for respond_in_session concurrent serialization."""

import asyncio

from api.chat import respond_in_session, set_store
from api.session_store import ChatSessionStore


def test_concurrent_respond_no_lost_update(tmp_path, monkeypatch):
    """Two concurrent calls on the same session must not overwrite each other."""
    store = ChatSessionStore(tmp_path / "sessions.db")
    set_store(store)
    session_id = store.create()

    from agent.session import AgentSession

    async def slow_respond(self, message: str) -> str:
        await asyncio.sleep(0.05)  # yield so both coroutines start before either saves
        self.messages.append({"role": "user", "content": message})
        return f"reply to {message}"

    monkeypatch.setattr(AgentSession, "respond", slow_respond)

    async def run():
        await asyncio.gather(
            respond_in_session(session_id, "hello"),
            respond_in_session(session_id, "world"),
        )

    asyncio.run(run())

    saved = store.load_messages(session_id)
    user_texts = [m["content"] for m in saved if m["role"] == "user"]
    assert "hello" in user_texts, f"'hello' missing from {user_texts}"
    assert "world" in user_texts, f"'world' missing from {user_texts}"
