"""Shared Server-Sent Events boilerplate for streaming endpoints.

Used by `/api/chat/sessions/{id}/messages/stream` (chat.py) and
`/api/departures/stream` (departures.py) — both are indefinite
`text/event-stream` responses that need the same no-buffering headers and the
same `data: ...\\n\\n` framing.
"""

from __future__ import annotations

import json

# `X-Accel-Buffering: no` disables nginx response buffering so events reach
# the client as they're produced instead of being held until the buffer fills.
SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


def sse_event(payload: dict | str) -> str:
    """Format one `data: ...\\n\\n` SSE frame.

    `payload` may be a dict (JSON-encoded here) or an already-serialized JSON
    string (e.g. Pydantic's `model_dump_json()`) — re-encoding a string would
    double-escape it.
    """
    body = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return f"data: {body}\n\n"
