"""Chat session endpoints.

In-process session store — suitable for single-machine kiosk. See CLAUDE.md
Gotcha section if scaling out to multiple workers is ever needed.
"""

from __future__ import annotations

import asyncio
import time
import uuid

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from agent.error import summarize_error
from agent.session import AgentSession
from config import Settings, make_agent_session

router = APIRouter()

_SESSION_TTL_SECONDS = 1800  # 30 min idle expiry

_chat_sessions: dict[str, AgentSession] = {}
_session_last_used: dict[str, float] = {}


def _make_agent_session() -> AgentSession:
    return make_agent_session(Settings.from_env())


def _purge_expired_sessions() -> None:
    now = time.time()
    expired = [
        sid
        for sid, last in _session_last_used.items()
        if now - last > _SESSION_TTL_SECONDS
    ]
    for sid in expired:
        _chat_sessions.pop(sid, None)
        _session_last_used.pop(sid, None)


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
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/api/chat/sessions", response_model=ChatSessionResponse)
def create_chat_session() -> object:
    """Create a new agent chat session. Returns a session_id for subsequent messages."""
    _purge_expired_sessions()

    try:
        session = _make_agent_session()
    except RuntimeError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error

    session_id = str(uuid.uuid4())
    _chat_sessions[session_id] = session
    _session_last_used[session_id] = time.time()
    return ChatSessionResponse(sessionId=session_id)


@router.post(
    "/api/chat/sessions/{session_id}/messages",
    response_model=ChatMessageResponse,
)
async def send_chat_message(session_id: str, body: ChatMessageRequest) -> object:
    """Send a message to an existing session. Runs agent in a thread (blocking I/O)."""
    session = _chat_sessions.get(session_id)
    if session is None:
        raise HTTPException(
            status_code=404, detail="對話階段不存在或已過期，請重新開始"
        )

    _session_last_used[session_id] = time.time()

    try:
        reply = await asyncio.to_thread(session.respond, body.message)
    except Exception as error:
        raise HTTPException(
            status_code=500,
            detail=f"助理暫時無法回應：{summarize_error(error)}",
        ) from error

    return {"reply": reply}


@router.delete("/api/chat/sessions/{session_id}", status_code=204)
def delete_chat_session(session_id: str) -> None:
    """Explicitly end a chat session and free its memory."""
    _chat_sessions.pop(session_id, None)
    _session_last_used.pop(session_id, None)
