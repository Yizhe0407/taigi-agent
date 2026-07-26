"""Frontend error/diagnostic reporting.

The kiosk runs unattended — mic permission failures, WebRTC ICE breakage,
uncaught JS exceptions were previously invisible to the backend. This is a
thin trust-boundary endpoint: validate, truncate, forward to the existing
diagnostics log (stdout + span event). No new metric instrument, no RUM
storage — see docs/observability.md.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, Field

from agent.diagnostics import log_diagnostic

from .request_limits import CLIENT_EVENT_RATE_LIMIT

router = APIRouter()

_MESSAGE_LIMIT = 500
_DETAIL_LIMIT = 2000


class ClientEventRequest(BaseModel):
    type: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1, max_length=_MESSAGE_LIMIT)
    detail: str | None = Field(default=None, max_length=_DETAIL_LIMIT)
    ts: float | None = None


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else f"{text[:limit]}…[truncated]"


@router.post("/api/client-events", status_code=204, dependencies=[Depends(CLIENT_EVENT_RATE_LIMIT)])
async def report_client_event(event: ClientEventRequest) -> Response:
    """Log a frontend-reported error/diagnostic event. Trust boundary: values are
    truncated and only ever used as log/span-event *values*, never as a format
    string, so attacker-controlled content can't inject format directives."""
    message = _truncate(event.message, _MESSAGE_LIMIT)
    parts = [event.type]
    if event.ts is not None:
        parts.append(f"ts={event.ts}")
    parts.append(message)
    if event.detail:
        parts.append(f"detail={_truncate(event.detail, _DETAIL_LIMIT)}")
    log_diagnostic(scope="client", message=" | ".join(parts))
    return Response(status_code=204)
