"""Cloudflare Access service-token header helpers.

The model services sit behind Cloudflare Access.  A service token is represented
by two headers, and both values are intentionally read at request/client setup
time rather than logged or embedded in source code.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

CLIENT_ID_HEADER = "CF-Access-Client-Id"
CLIENT_SECRET_HEADER = "CF-Access-Client-Secret"


def access_headers(
    client_id: str | None = None,
    client_secret: str | None = None,
) -> dict[str, str]:
    """Return Cloudflare Access headers when a complete credential is configured.

    Passing both arguments is useful for immutable config objects (for example
    TTSConfig); omitting them reads the process environment.  An unset pair is
    valid for local/self-hosted deployments.  A partial pair is rejected rather
    than silently sending an unusable request.
    """
    resolved_id = (client_id if client_id is not None else os.getenv("CF_ACCESS_CLIENT_ID", "")).strip()
    resolved_secret = (
        client_secret if client_secret is not None else os.getenv("CF_ACCESS_CLIENT_SECRET", "")
    ).strip()
    if not resolved_id and not resolved_secret:
        return {}
    if not resolved_id or not resolved_secret:
        raise RuntimeError("CF_ACCESS_CLIENT_ID and CF_ACCESS_CLIENT_SECRET must be set together")
    return {
        CLIENT_ID_HEADER: resolved_id,
        CLIENT_SECRET_HEADER: resolved_secret,
    }


def merge_access_headers(
    headers: Mapping[str, str] | None = None,
    *,
    client_id: str | None = None,
    client_secret: str | None = None,
) -> dict[str, str]:
    """Copy *headers* and add the configured Cloudflare Access headers."""
    merged = dict(headers or {})
    merged.update(access_headers(client_id, client_secret))
    return merged
