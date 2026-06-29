"""Chat session endpoints.

Session messages persist in a SQLite store so `--reload` and crashes don't
drop in-flight conversations. `AgentSession` itself is rebuilt per request
from `Settings`; only the mutable message log is persisted (see
`api.session_store`).
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from agent.error import summarize_error
from agent.session import AgentSession
from api.session_store import ChatSessionStore
from config import get_settings, make_agent_session

router = APIRouter()


# ---------------------------------------------------------------------------
# Store lifecycle
# ---------------------------------------------------------------------------


_store: ChatSessionStore | None = None


def _get_store() -> ChatSessionStore:
    """Return the process-wide chat session store, creating it on first use.

    Lazy so import-time side effects (mkdir, sqlite open) only fire when the
    chat endpoints are actually hit — keeps imports cheap for non-chat tests.
    """
    global _store
    if _store is None:
        path = Path(os.getenv("CHAT_SESSION_DB", ".agent_state/sessions.db"))
        _store = ChatSessionStore(path)
    return _store


def set_store(store: ChatSessionStore) -> None:
    """Inject a store (tests, alternative backends)."""
    global _store
    _store = store


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class ChatSessionResponse(BaseModel):
    # Intentionally camelCase to match the JSON key the frontend expects.
    sessionId: str  # noqa: N815


class ChatMessageRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)


class ChatMessageResponse(BaseModel):
    reply: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _rehydrate_session(messages: list[dict]) -> AgentSession:
    session = make_agent_session(get_settings())
    session.messages = messages
    return session


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/api/chat/sessions", response_model=ChatSessionResponse)
def create_chat_session() -> object:
    """Create a new agent chat session and return its session_id."""
    try:
        # Surface missing LLM config now rather than on first message.
        get_settings()
    except RuntimeError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error

    session_id = _get_store().create()
    return ChatSessionResponse(sessionId=session_id)


@router.post(
    "/api/chat/sessions/{session_id}/messages",
    response_model=ChatMessageResponse,
)
async def send_chat_message(session_id: str, body: ChatMessageRequest) -> object:
    """Append a user message, run the agent, persist updated history."""
    store = _get_store()

    try:
        # SQLite I/O is synchronous — run it off the event loop so a slow disk
        # can't stall other in-flight requests.
        messages = await asyncio.to_thread(store.load_messages, session_id)
        if messages is None:
            raise LookupError(session_id)
        session = _rehydrate_session(messages)
        reply = await session.respond(body.message)
        await asyncio.to_thread(store.save_messages, session_id, session.messages)
    except LookupError as error:
        raise HTTPException(status_code=404, detail="對話階段不存在或已過期，請重新開始") from error
    except Exception as error:
        raise HTTPException(
            status_code=500,
            detail=f"助理暫時無法回應：{summarize_error(error)}",
        ) from error

    return {"reply": reply}


@router.delete("/api/chat/sessions/{session_id}", status_code=204)
def delete_chat_session(session_id: str) -> None:
    """Explicitly end a chat session and free its row."""
    _get_store().delete(session_id)
