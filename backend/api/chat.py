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
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from agent.error import summarize_error
from agent.session import AgentSession
from agent.tool_dispatch import ToolHandler
from api.session_store import ChatSessionStore
from config import get_settings, make_agent_session

from .request_limits import CHAT_MESSAGE_RATE_LIMIT, CHAT_SESSION_RATE_LIMIT
from .sse import SSE_HEADERS, sse_event

# (schema, handler) pair injected into a single session at rehydration time —
# used by the voice pipeline to add per-connection tools (e.g. end_conversation)
# that must not enter the global TOOL_SCHEMAS/TOOL_HANDLERS (no REST UI for them).
ExtraTool = tuple[dict, ToolHandler]

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
_session_locks: dict[str, asyncio.Lock] = {}
# Holders + waiters per session_id. A Lock with a non-zero count must never be
# removed from `_session_locks` (see `_discard_session_lock`).
_session_lock_users: dict[str, int] = {}


def get_store() -> ChatSessionStore:
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


def close_store() -> None:
    """Close the process-wide store's sqlite connection (lifespan shutdown).

    No-op when no store was ever created, so shutting down a process that
    never served chat doesn't materialise a sqlite file on the way out.
    """
    global _store
    if _store is not None:
        _store.close()
        _store = None


@asynccontextmanager
async def _hold_session_lock(session_id: str) -> AsyncIterator[None]:
    """Acquire (creating on demand) the per-session write lock.

    The user count is bumped *before* awaiting the Lock so that a purge pass
    running while we're still queued can't swap the Lock object out from under
    us — that would hand the next caller a second, unrelated Lock for the same
    session and quietly destroy mutual exclusion.
    """
    lock = _session_locks.get(session_id)
    if lock is None:
        lock = asyncio.Lock()
        _session_locks[session_id] = lock
    _session_lock_users[session_id] = _session_lock_users.get(session_id, 0) + 1
    try:
        async with lock:
            yield
    finally:
        remaining = _session_lock_users.get(session_id, 1) - 1
        if remaining > 0:
            _session_lock_users[session_id] = remaining
        else:
            _session_lock_users.pop(session_id, None)


def _discard_session_lock(session_id: str) -> None:
    """Drop a session's Lock unless somebody is holding or waiting on it.

    An in-use Lock is left in place and reclaimed by a later purge pass; the
    entry is bounded either way because the session's row is already gone.
    """
    if _session_lock_users.get(session_id):
        return
    _session_locks.pop(session_id, None)


async def purge_expired_locks() -> None:
    """Reconcile `_session_locks` against the session rows that still exist.

    Must diff against the live id set rather than trust `purge_expired()`'s
    return value: `load_messages()` also deletes an expired row in place
    without reporting it (hit on every voice reconnect with a stale
    session_id), which used to leak that session's Lock for the process's
    whole uptime.
    """
    store = get_store()
    await asyncio.to_thread(store.purge_expired)
    live_ids = await asyncio.to_thread(store.session_ids)
    # No awaits below: `_session_locks` and the id snapshot can't drift apart
    # mid-iteration. A session created *after* the snapshot is safe too — its
    # Lock is either absent or in use, and in-use locks are never dropped.
    for session_id in [sid for sid in _session_locks if sid not in live_ids]:
        _discard_session_lock(session_id)


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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _rehydrate_session(
    messages: list[dict],
    *,
    extra_tools: list[ExtraTool] | None = None,
    extra_system_prompt: str | None = None,
) -> AgentSession:
    """Rebuild an AgentSession from persisted messages for this one request.

    Only `messages` round-trips through the store — `conv_state` always comes
    back as ConvState() (last_intent=None). Harmless today since
    IntentRouter.classify() only writes next_state and never reads it back,
    but a future rule that reads ConvState across turns would silently never
    see a non-default value; persist it in ChatSessionStore if that happens.

    `extra_tools` / `extra_system_prompt` layer per-session additions on top
    of the global tool set as fresh list/dict copies, so the injection can't
    leak into other sessions via the module-level TOOL_SCHEMAS/TOOL_HANDLERS.
    """
    session = make_agent_session(get_settings())
    session.messages = messages
    if extra_tools:
        session.tool_schemas = [*session.tool_schemas, *(schema for schema, _ in extra_tools)]
        session.tool_handlers = {
            **session.tool_handlers,
            **{schema["function"]["name"]: handler for schema, handler in extra_tools},
        }
    if extra_system_prompt:
        session.system_prompt = session.system_prompt + extra_system_prompt
    return session


async def respond_in_session_stream(
    session_id: str,
    message: str,
    *,
    extra_tools: list[ExtraTool] | None = None,
    extra_system_prompt: str | None = None,
):
    """Load session, stream reply chunks, then save updated history.

    Raises LookupError (before the first chunk) if the session does not exist.
    Holds the session lock for the whole stream to serialize concurrent calls.
    History saves only after the stream完整跑完——中途放棄（語音打斷、client
    斷線）就丟棄該輪，與語音中斷語意一致。

    `extra_tools` / `extra_system_prompt` are the voice pipeline's injection
    hooks (see `_rehydrate_session`). REST callers pass neither, so their
    behaviour is unchanged.
    """
    store = get_store()
    async with _hold_session_lock(session_id):
        # SQLite I/O is synchronous — run it off the event loop
        messages = await asyncio.to_thread(store.load_messages, session_id)
        if messages is None:
            # Session gone (expired/never existed). Deliberately *not* popping
            # the Lock here: we still hold it, and another caller may already be
            # queued on it — dropping it now would let that caller's successor
            # build a rival Lock for the same session. purge_expired_locks()
            # reclaims it once nobody is using it.
            raise LookupError(session_id)

        session = _rehydrate_session(
            messages,
            extra_tools=extra_tools,
            extra_system_prompt=extra_system_prompt,
        )
        async for chunk in session.respond_stream(message):
            yield chunk
        await asyncio.to_thread(store.save_messages, session_id, session.messages)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/api/chat/sessions",
    response_model=ChatSessionResponse,
    dependencies=[Depends(CHAT_SESSION_RATE_LIMIT)],
)
def create_chat_session() -> object:
    """Create a new agent chat session and return its session_id."""
    try:
        # Surface missing LLM config now rather than on first message.
        get_settings()
    except RuntimeError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error

    session_id = get_store().create()
    return ChatSessionResponse(sessionId=session_id)


@router.post(
    "/api/chat/sessions/{session_id}/messages/stream",
    dependencies=[Depends(CHAT_MESSAGE_RATE_LIMIT)],
)
async def send_chat_message_stream(session_id: str, body: ChatMessageRequest) -> StreamingResponse:
    """SSE 逐 chunk 推回覆。

    事件格式：`{"delta": 文字}`… 結尾 `{"done": true}`；串流中的錯誤以
    `{"error": 訊息}` 事件收尾（HTTP status 已送出，無法改）。
    """
    # SSE 開始後無法再改 status code，session 存在與否先查（與串流開始之間
    # 的過期 race 由 error 事件兜底）。輕量 exists() 只查 last_used，不重覆
    # 載入/解析 messages payload——權威讀在 respond_in_session_stream 的 lock 內。
    if not await asyncio.to_thread(get_store().exists, session_id):
        raise HTTPException(status_code=404, detail="對話階段不存在或已過期，請重新開始")

    async def events():
        try:
            async for chunk in respond_in_session_stream(session_id, body.message):
                yield sse_event({"delta": chunk})
            yield sse_event({"done": True})
        except LookupError:
            yield sse_event({"error": "對話階段不存在或已過期，請重新開始"})
        except Exception as error:  # noqa: BLE001 — must surface as an SSE event
            _log.exception("Chat stream failed for session %s", session_id)
            yield sse_event({"error": f"助理暫時無法回應：{summarize_error(error)}"})

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


@router.delete("/api/chat/sessions/{session_id}", status_code=204)
def delete_chat_session(session_id: str) -> None:
    """Explicitly end a chat session and free its row."""
    get_store().delete(session_id)
    _discard_session_lock(session_id)
