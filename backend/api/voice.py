"""WebRTC voice endpoint.

POST /api/voice/offer   — SDP offer/answer exchange (SmallWebRTCRequestHandler)
PATCH /api/voice/offer  — Trickle ICE candidate（若需要）
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException
from pipecat.transports.smallwebrtc.request_handler import (
    IceCandidate,
    SmallWebRTCPatchRequest,
    SmallWebRTCRequest,
    SmallWebRTCRequestHandler,
)

from agent.diagnostics import log_diagnostic
from api.chat import _get_store

from .request_limits import VOICE_RATE_LIMIT

router = APIRouter()
_log = logging.getLogger(__name__)

# Process-wide handler — manages all active SmallWebRTC connections.
_handler = SmallWebRTCRequestHandler()

# Per-connection run_voice_pipeline() background tasks, tracked so graceful
# shutdown (see shutdown() below) can wait for / cancel them instead of
# leaving them orphaned when the process exits.
_pipeline_tasks: set[asyncio.Task] = set()


async def shutdown() -> None:
    """Close all active WebRTC connections and stop their pipeline tasks.

    Called from api/__init__.py's lifespan shutdown. `_handler.close()`
    disconnects every tracked peer connection, which fires each connection's
    on_client_disconnected handler (voice/pipeline.py) and cancels its
    PipelineWorker — that's normally enough for run_voice_pipeline() to return
    on its own. The explicit cancel+gather below is a safety net for any
    pipeline task that doesn't unwind from that alone (e.g. it crashed before
    the transport finished connecting), so no per-connection task is ever left
    running past process shutdown.
    """
    await _handler.close()
    tasks = [t for t in _pipeline_tasks if not t.done()]
    for t in tasks:
        t.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


@router.post("/api/voice/offer", dependencies=[Depends(VOICE_RATE_LIMIT)])
async def webrtc_offer(body: dict) -> dict:
    """Exchange SDP offer/answer and start (or resume) the voice pipeline.

    The client sends its SDP offer; this endpoint returns the server's SDP answer.
    SmallWebRTCRequestHandler handles connection reuse and lifecycle.

    Body: { sdp, type, pc_id?, session_id? }
    Returns: { sdp, type, pc_id }

    session_id should be the existing chat session created by the frontend so that
    voice and text interactions share a single conversation context.
    If omitted, a new session is created as a fallback (e.g. voice-only mode).
    """
    # Prefer the session_id passed by the frontend so voice and text share context.
    # Only create a new session if the client didn't supply one.
    client_session_id: str | None = body.pop("session_id", None)

    try:
        request = SmallWebRTCRequest.from_dict(body)
    except (TypeError, KeyError) as exc:
        raise HTTPException(status_code=422, detail=f"Invalid request body: {exc}") from exc

    async def _start_pipeline(connection) -> None:
        """Spin up voice pipeline in background so this endpoint returns immediately.

        Session resolution stays inside this callback on purpose: pipecat only
        invokes it for NEW connections. Renegotiations (existing pc_id) never
        reach here, so resolving the session earlier would create an orphaned
        session per renegotiation. Store failures are logged by the handler
        and by the pipeline done-callback below.
        """
        from voice.pipeline import run_voice_pipeline

        store = _get_store()
        if client_session_id and await asyncio.to_thread(store.load_messages, client_session_id) is not None:
            session_id = client_session_id
            _log.info("Voice pipeline reusing existing session %s", session_id)
        else:
            session_id = await asyncio.to_thread(store.create)
            _log.info("Voice pipeline created new session %s (no valid session_id from client)", session_id)

        def _on_pipeline_done(fut: asyncio.Task) -> None:
            if fut.cancelled() or fut.exception() is None:
                return
            exc = fut.exception()
            _log.exception("Voice pipeline error", exc_info=exc)
            log_diagnostic("voice.pipeline", f"session={session_id} _start_pipeline task crashed: {exc}")

        t = asyncio.create_task(run_voice_pipeline(connection, session_id))
        _pipeline_tasks.add(t)
        t.add_done_callback(_on_pipeline_done)
        t.add_done_callback(_pipeline_tasks.discard)

    try:
        answer = await _handler.handle_web_request(request, _start_pipeline)
    except HTTPException:
        raise
    except Exception as exc:
        _log.exception("WebRTC negotiation error")
        raise HTTPException(status_code=500, detail=f"WebRTC negotiation failed: {exc}") from exc

    if answer is None:
        raise HTTPException(status_code=500, detail="No SDP answer produced")

    return answer


@router.patch("/api/voice/offer", dependencies=[Depends(VOICE_RATE_LIMIT)])
async def webrtc_patch(body: dict) -> None:
    """Accept trickle ICE candidates from the client (optional, for faster connectivity)."""
    try:
        request = SmallWebRTCPatchRequest(
            pc_id=body["pc_id"],
            candidates=[IceCandidate(**c) for c in body.get("candidates", [])],
        )
    except (TypeError, KeyError) as exc:
        raise HTTPException(status_code=422, detail=f"Invalid request body: {exc}") from exc

    try:
        await _handler.handle_patch_request(request)
    except HTTPException:
        raise
    except Exception as exc:
        _log.exception("ICE candidate error")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
