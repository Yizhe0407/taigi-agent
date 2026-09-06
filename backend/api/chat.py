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
from collections.abc import AsyncGenerator, AsyncIterator, Iterator
from contextlib import aclosing, asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from agent.error import summarize_error
from agent.session import AgentSession
from agent.tool_dispatch import ToolHandler
from api.session_store import ChatSessionStore, SessionTombstonedError
from async_lifecycle import create_lifecycle_task, join_task, run_in_thread
from config import get_settings, make_agent_session

from .request_limits import CHAT_MESSAGE_RATE_LIMIT, CHAT_SESSION_RATE_LIMIT
from .sse import SSE_HEADERS, sse_event

# (schema, handler) pair injected into a single session at rehydration time —
# used by the voice pipeline to add per-connection tools (e.g. end_conversation)
# that must not enter the global TOOL_SCHEMAS/TOOL_HANDLERS (no REST UI for them).
ExtraTool = tuple[dict, ToolHandler]

router = APIRouter()
_log = logging.getLogger(__name__)

# How often the background loop reconciles session lock states against expired
# sessions. Independent of the store's own TTL — this only bounds how long a
# dangling Lock can survive after its session expires.
_LOCK_PURGE_INTERVAL_SECONDS = 300.0


# ---------------------------------------------------------------------------
# Store lifecycle
# ---------------------------------------------------------------------------


@dataclass
class _SessionLockState:
    """One session lock and all lifecycle metadata owned with it."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0
    retire_when_idle: bool = False


class _ChatStoreLease:
    """One admitted operation owned by exactly one chat-store generation."""

    __slots__ = ("_owner", "_released")

    def __init__(self, owner: _ChatStoreRuntime) -> None:
        self._owner = owner
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._owner._release_operation(self)


class _ChatStoreRuntime:
    """Authoritative owner for one SQLite store and its session locks.

    The runtime is published before opening SQLite so partial construction always
    has one reachable owner. Admission closes permanently before physical close,
    shutdown waits for every admitted operation, and failed close retains this
    exact store generation for a later retry. A successor may only be installed
    by explicit startup after this runtime reports successful teardown.
    """

    def __init__(self, store: ChatSessionStore) -> None:
        self.store = store
        self.session_lock_states: dict[str, _SessionLockState] = {}
        self._operations: set[_ChatStoreLease] = set()
        self._operations_empty = asyncio.Event()
        self._operations_empty.set()
        self._ready = False
        self._closing = False
        self._closed = False
        self._shutdown_task: asyncio.Task[None] | None = None

    @property
    def ready(self) -> bool:
        return self._ready and not self._closing and not self._closed

    @property
    def closing(self) -> bool:
        return self._closing

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def active_operation_count(self) -> int:
        return len(self._operations)

    @property
    def shutdown_task(self) -> asyncio.Task[None] | None:
        return self._shutdown_task

    def require_accepting(self) -> None:
        if not self.ready or not self.store.opened:
            raise HTTPException(status_code=503, detail="Chat session store is unavailable")

    def acquire_operation(self) -> _ChatStoreLease:
        self.require_accepting()
        lease = _ChatStoreLease(self)
        self._operations.add(lease)
        self._operations_empty.clear()
        return lease

    def _release_operation(self, lease: _ChatStoreLease) -> None:
        self._operations.discard(lease)
        if not self._operations:
            self._operations_empty.set()

    async def open(self) -> None:
        if self._ready:
            return
        if self._closing or self._closed:
            raise RuntimeError("Chat session store generation is closed")

        try:
            await run_in_thread(self.store.open)
            if self._closing:
                raise RuntimeError("Chat session store closed while opening")
        except BaseException as primary:
            try:
                await self.aclose()
            except BaseException as cleanup_error:
                raise BaseExceptionGroup(
                    "Chat session store initialization and rollback both failed",
                    [primary, cleanup_error],
                ) from None
            raise
        self._ready = True

    async def _finalize(self) -> None:
        await self._operations_empty.wait()
        await run_in_thread(self.store.close)
        self._ready = False
        self.session_lock_states.clear()

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closing = True
        task = self._shutdown_task
        if task is None:
            task = create_lifecycle_task(
                self._finalize(),
                name="chat-session-store-shutdown",
            )
            self._shutdown_task = task

        try:
            await join_task(task)
        finally:
            if self._shutdown_task is task and task.done():
                if not task.cancelled() and task.exception() is None:
                    self._closed = True
                self._shutdown_task = None


_chat_store_runtime: _ChatStoreRuntime | None = None


async def startup_store(store: ChatSessionStore | None = None) -> None:
    """Explicitly install and open one chat-store generation."""
    global _chat_store_runtime
    previous = _chat_store_runtime
    if previous is not None and not previous.closed:
        raise RuntimeError("Cannot replace chat session store before shutdown succeeds")

    candidate = store
    if candidate is None:
        path = Path(os.getenv("CHAT_SESSION_DB", ".agent_state/sessions.db"))
        candidate = ChatSessionStore(path)
    runtime = _ChatStoreRuntime(candidate)
    _chat_store_runtime = runtime
    await runtime.open()


def _require_chat_store_runtime() -> _ChatStoreRuntime:
    runtime = _chat_store_runtime
    if runtime is None:
        raise HTTPException(status_code=503, detail="Chat session store is not started")
    runtime.require_accepting()
    return runtime


@contextmanager
def chat_store_operation(
    runtime: _ChatStoreRuntime | None = None,
) -> Iterator[_ChatStoreRuntime]:
    owner = runtime if runtime is not None else _require_chat_store_runtime()
    lease = owner.acquire_operation()
    try:
        yield owner
    finally:
        lease.release()


async def close_store() -> None:
    """Close the installed generation without discarding failed ownership."""
    runtime = _chat_store_runtime
    if runtime is None:
        return
    await runtime.aclose()


@asynccontextmanager
async def _hold_session_lock(
    runtime: _ChatStoreRuntime,
    session_id: str,
) -> AsyncIterator[None]:
    """Acquire (creating on demand) the per-session write lock.

    The user count is bumped *before* awaiting the Lock so that a purge pass
    running while we're still queued can't swap the Lock object out from under
    us — that would hand the next caller a second, unrelated Lock for the same
    session and quietly destroy mutual exclusion.
    """
    states = runtime.session_lock_states
    state = states.get(session_id)
    if state is None:
        state = _SessionLockState()
        states[session_id] = state
    state.users += 1
    try:
        async with state.lock:
            yield
    finally:
        state.users -= 1
        if state.users == 0 and state.retire_when_idle and states.get(session_id) is state:
            states.pop(session_id, None)


def _retire_session_lock(runtime: _ChatStoreRuntime, session_id: str) -> None:
    """Retire a dead session's lock immediately after its final user leaves.

    The state remains the sole lock while holders or waiters exist, preventing a
    rival lock from breaking mutual exclusion. The final user's ``finally`` owns
    deterministic reclamation, so no periodic second pass is required.
    """
    states = runtime.session_lock_states
    state = states.get(session_id)
    if state is None:
        return
    state.retire_when_idle = True
    if state.users == 0:
        states.pop(session_id, None)


def _mark_session_lock_live(runtime: _ChatStoreRuntime, session_id: str) -> None:
    """Cancel retirement when the same client ID becomes a live session again."""
    state = runtime.session_lock_states.get(session_id)
    if state is not None:
        state.retire_when_idle = False


async def purge_expired_locks() -> None:
    """Reconcile this generation's lock states against its durable rows."""
    with chat_store_operation() as runtime:
        store = runtime.store
        await run_in_thread(store.purge_expired)
        live_ids = await run_in_thread(store.session_ids)
        # No awaits below: the lock registry and id snapshot cannot drift during
        # reconciliation, and in-use states retire only after their final user.
        states = runtime.session_lock_states
        for session_id in [sid for sid in states if sid not in live_ids]:
            _retire_session_lock(runtime, session_id)


async def run_lock_purge_loop() -> None:
    """Periodically reconcile the session-lock state registry with the store.

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


def respond_in_session_stream(
    session_id: str,
    message: str,
    *,
    extra_tools: list[ExtraTool] | None = None,
    extra_system_prompt: str | None = None,
    runtime: _ChatStoreRuntime | None = None,
) -> AsyncGenerator[str, None]:
    """Capture one store generation, stream a reply, then persist its history."""
    captured_runtime = runtime if runtime is not None else _require_chat_store_runtime()

    async def _stream() -> AsyncGenerator[str, None]:
        with chat_store_operation(captured_runtime) as active_runtime:
            store = active_runtime.store
            async with _hold_session_lock(active_runtime, session_id):
                messages = await run_in_thread(store.load_messages, session_id)
                if messages is None:
                    _retire_session_lock(active_runtime, session_id)
                    raise LookupError(session_id)

                session = _rehydrate_session(
                    messages,
                    extra_tools=extra_tools,
                    extra_system_prompt=extra_system_prompt,
                )
                response_stream = session.respond_stream(message)
                async with aclosing(response_stream):
                    async for chunk in response_stream:
                        yield chunk
                await run_in_thread(store.save_messages, session_id, session.messages)

    return _stream()


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.put(
    "/api/chat/sessions/{session_id}",
    response_model=ChatSessionResponse,
    dependencies=[Depends(CHAT_SESSION_RATE_LIMIT)],
)
async def create_chat_session(session_id: UUID) -> ChatSessionResponse:
    """Idempotently materialise one client-owned chat session ID."""
    try:
        # Surface missing LLM config now rather than on first message.
        get_settings()
    except RuntimeError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error

    session_key = str(session_id)
    try:
        with chat_store_operation() as runtime:
            await run_in_thread(runtime.store.create, session_key)
            _mark_session_lock_live(runtime, session_key)
    except SessionTombstonedError as error:
        raise HTTPException(status_code=409, detail="對話階段已明確結束，請使用新的識別碼") from error
    return ChatSessionResponse(sessionId=session_key)


@router.post(
    "/api/chat/sessions/{session_id}/messages/stream",
    dependencies=[Depends(CHAT_MESSAGE_RATE_LIMIT)],
)
async def send_chat_message_stream(session_id: UUID, body: ChatMessageRequest) -> StreamingResponse:
    """SSE 逐 chunk 推回覆。

    事件格式：`{"delta": 文字}`… 結尾 `{"done": true}`；串流中的錯誤以
    `{"error": 訊息}` 事件收尾（HTTP status 已送出，無法改）。
    """
    session_key = str(session_id)
    # SSE 開始後無法再改 status code，session 存在與否先查（與串流開始之間
    # 的過期 race 由 error 事件兜底）。輕量 exists() 只查 last_used，不重覆
    # 載入/解析 messages payload——權威讀在 respond_in_session_stream 的 lock 內。
    with chat_store_operation() as runtime:
        if not await run_in_thread(runtime.store.exists, session_key):
            raise HTTPException(status_code=404, detail="對話階段不存在或已過期，請重新開始")

    async def events() -> AsyncGenerator[str, None]:
        response_stream = respond_in_session_stream(
            session_key,
            body.message,
            runtime=runtime,
        )
        try:
            async with aclosing(response_stream):
                async for chunk in response_stream:
                    yield sse_event({"delta": chunk})
            yield sse_event({"done": True})
        except LookupError:
            yield sse_event({"error": "對話階段不存在或已過期，請重新開始"})
        except Exception as error:  # noqa: BLE001 — must surface as an SSE event
            _log.exception("Chat stream failed for session %s", session_key)
            yield sse_event({"error": f"助理暫時無法回應：{summarize_error(error)}"})

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


@router.delete(
    "/api/chat/sessions/{session_id}",
    status_code=204,
    dependencies=[Depends(CHAT_SESSION_RATE_LIMIT)],
)
async def delete_chat_session(session_id: UUID) -> None:
    """Explicitly end a chat session and durably prevent late recreation."""
    session_key = str(session_id)
    # The sqlite connection is thread-safe and all of its blocking operations
    # run outside the event loop.  The asyncio Lock registry, however, is owned
    # by this loop and must never be mutated from FastAPI's sync-handler pool.
    with chat_store_operation() as runtime:
        await run_in_thread(runtime.store.delete, session_key)
        _retire_session_lock(runtime, session_key)
