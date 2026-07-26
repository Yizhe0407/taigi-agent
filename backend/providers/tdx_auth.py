"""Shared TDX OAuth2 client_credentials token client.

Both `TdxBusProvider` (providers/tdx_bus.py) and `TdxBikeProvider`
(providers/moovo.py) authenticate against the same TDX OAuth endpoint with
the same scheme: fetch + cache a bearer token, refresh it slightly before
expiry, and — since TDX will occasionally reject an unexpired-by-our-clock
token with a 401 (revoked/rotated server-side) — force a refresh and retry
the request once. This module owns that logic so both providers (and any
future TDX-backed provider) share it instead of re-implementing it.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

import httpx

from providers.http import get_http_client

_TOKEN_URL = "https://tdx.transportdata.tw/auth/realms/TDXConnect/protocol/openid-connect/token"
_TOKEN_BUFFER_SECONDS = 60.0  # refresh this many seconds before the token actually expires
_DEFAULT_TIMEOUT_SECONDS = 10.0


class TdxTokenClient:
    """Fetches and caches a TDX OAuth2 client_credentials token for one credential pair."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        clock: Callable[[], float] = time.monotonic,
        timeout: float = _DEFAULT_TIMEOUT_SECONDS,
        token_url: str = _TOKEN_URL,
        http_client_factory: Callable[[], httpx.AsyncClient] = get_http_client,
        record_hit: Callable[[bool], None] | None = None,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._clock = clock
        self._timeout = timeout
        self._token_url = token_url
        self._http_client_factory = http_client_factory
        self._record_hit = record_hit
        self._token: str = ""
        self._token_expires_at: float = 0.0
        self._lock = asyncio.Lock()

    async def get_token(self) -> str:
        hit = self._clock() < self._token_expires_at
        if self._record_hit is not None:
            self._record_hit(hit)
        if hit:
            return self._token
        async with self._lock:
            # Re-check after acquiring: first caller refreshes, subsequent callers reuse it.
            if self._clock() < self._token_expires_at:
                return self._token
            await self._refresh()
            return self._token

    def invalidate(self) -> None:
        """Force the next `get_token()` call to fetch a fresh token."""
        self._token_expires_at = 0.0

    async def request_with_retry(self, do_request: Callable[[str], Awaitable[httpx.Response]]) -> httpx.Response:
        """Call `do_request(token)`; on HTTP 401 force-refresh the token and retry once.

        Independent of any caller-side rate-limit retry budget — a 401
        refresh must not consume a 429 retry attempt.
        """
        token = await self.get_token()
        resp = await do_request(token)
        if resp.status_code == 401:
            self.invalidate()
            token = await self.get_token()
            resp = await do_request(token)
        return resp

    async def _refresh(self) -> None:
        http = self._http_client_factory()
        resp = await http.post(
            self._token_url,
            data={
                "grant_type": "client_credentials",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            },
            timeout=self._timeout,
        )
        resp.raise_for_status()
        body = resp.json()
        token = body.get("access_token")
        if not isinstance(token, str) or not token:
            raise ValueError("TDX token response has no access_token")
        expires_in = body.get("expires_in")
        ttl = float(expires_in) if isinstance(expires_in, int | float) else 3600.0
        self._token = token
        self._token_expires_at = self._clock() + max(0.0, ttl - _TOKEN_BUFFER_SECONDS)
