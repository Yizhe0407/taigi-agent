"""Process-wide shared `httpx.AsyncClient`.

Creating an AsyncClient per request discards the connection pool, so every
upstream call pays a fresh TCP + TLS handshake. One persistent client shared
by all upstreams (TTS / ASR / TDX / OTP) reuses connections; callers pass
their own per-request `timeout=` so the upstream-specific limits stay where
the call is made.

`api/__init__._lifespan` closes the client on shutdown. Tests monkeypatch
`get_http_client` (or the importing module's reference to it) to inject fakes.
"""

from __future__ import annotations

import httpx

_DEFAULT_TIMEOUT = 20.0

_client: httpx.AsyncClient | None = None


def get_http_client() -> httpx.AsyncClient:
    """Return the shared AsyncClient, creating it on first use."""
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT)
    return _client


async def aclose_http_client() -> None:
    """Close the shared client (app shutdown / test teardown)."""
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None
