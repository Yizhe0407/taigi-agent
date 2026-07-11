"""Chat session endpoints.

Session messages persist in a SQLite store so `--reload` and crashes don't
drop in-flight conversations. `AgentSession` itself is rebuilt per request
from `Settings`; only the mutable message log is persisted (see
`api.session_store`).
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from agent.error import summarize_error
from agent.session import AgentSession
from api.session_store import ChatSessionStore
from config import get_settings, make_agent_session

router = APIRouter()
_log = logging.getLogger(__name__)

# How often the background loop reconciles _session_locks against expired
# sessions. Independent of the store's own TTL — this only bounds how long a
# dangling Lock can survive after its session expires.
_LOCK_PURGE_INTERVAL_SECONDS = 300.0


# ---------------------------------------------------------------------------
# Store lifecycle
# ---------------------------------------------------------------------------


_store: ChatSessionStore | None = None
_session_locks: dict[str, asyncio.Lock] = {}  # ponytail: module-level dict, pop on delete to avoid unbounded growth


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


async def purge_expired_locks() -> None:
    """Drop `_session_locks` entries for sessions the store just expired.

    `respond_in_session` only cleans up a session's lock when that same
    session_id is looked up again after expiry (the LookupError path) — a
    session created but never revisited leaves its Lock behind forever.
    Over a long kiosk uptime with steady session churn that grows unbounded.
    """
    store = _get_store()
    purged_ids = await asyncio.to_thread(store.purge_expired)
    for session_id in purged_ids:
        _session_locks.pop(session_id, None)


async def run_lock_purge_loop() -> None:
    """Background loop: periodically reconcile `_session_locks` with the store.

    Started alongside the ETA warmup loop in `api._lifespan`.
    """
    while True:
        await asyncio.sleep(_LOCK_PURGE_INTERVAL_SECONDS)
        try:
            await purge_expired_locks()
        except Exception as exc:  # noqa: BLE001 — background loop must not die
            _log.warning("Session lock purge failed: %s", exc)


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
    """Rebuild an AgentSession from persisted messages for this one request.

    Only `messages` round-trips through the store — `conv_state` always comes
    back as ConvState() (last_intent=None), so any router rule that reads
    ConvState across turns will silently never see a non-default value here.
    Currently harmless: IntentRouter.classify() only *writes* next_state,
    nothing reads it back. If a future rule starts reading it, persist it
    alongside messages in ChatSessionStore.
    """
    session = make_agent_session(get_settings())
    session.messages = messages
    return session


async def respond_in_session(session_id: str, message: str) -> str:
    """Load session, respond to message, and save updated history.

    Raises LookupError if the session does not exist.
    Serializes concurrent calls on the same session_id to prevent lost updates.
    """
    store = _get_store()
    lock = _session_locks.setdefault(session_id, asyncio.Lock())
    async with lock:
        # SQLite I/O is synchronous — run it off the event loop
        messages = await asyncio.to_thread(store.load_messages, session_id)
        if messages is None:
            # Session gone (expired/never existed) — drop its lock too, otherwise
            # _session_locks grows unbounded for every expired session_id ever seen.
            _session_locks.pop(session_id, None)
            raise LookupError(session_id)

        session = _rehydrate_session(messages)
        reply = await session.respond(message)
        await asyncio.to_thread(store.save_messages, session_id, session.messages)
    return reply


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
    try:
        reply = await respond_in_session(session_id, body.message)
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
    _session_locks.pop(session_id, None)
