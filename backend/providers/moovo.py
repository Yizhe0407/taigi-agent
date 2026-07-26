"""TDX Bike v2 provider — HTTP fetch + OAuth token cache.

Only knows about TDX endpoints / token semantics. Parsing, dataclasses,
caching policy, and spatial queries live in `services.moovo` so domain
logic stays decoupled from transport.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Callable
from typing import Any, Protocol

import httpx

from providers.http import get_http_client
from providers.tdx_auth import TdxTokenClient
from telemetry import get_telemetry

_DEFAULT_BIKE_BASE_URL = "https://tdx.transportdata.tw/api/basic/v2/Bike"
_DEFAULT_CITY = "YunlinCounty"
_REQUEST_TIMEOUT_SECONDS = 20.0


class MoovoError(RuntimeError):
    """Raised when MOOVO bike-station data cannot be returned."""


class MoovoConfigError(MoovoError):
    """Raised when TDX credentials are not configured."""


class MoovoApiError(MoovoError):
    """Raised when TDX Bike API returns unusable data."""


class BikeProvider(Protocol):
    """Read-only view of a city's public bike-share (MOOVO) stations."""

    async def fetch_station_payloads(self) -> tuple[list[Any], list[Any]]:
        """Return raw (stations, availability) TDX Bike v2 payloads."""
        ...


def _tdx_credentials() -> tuple[str, str]:
    client_id = os.getenv("TDX_CLIENT_ID")
    client_secret = os.getenv("TDX_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise MoovoConfigError("TDX_CLIENT_ID / TDX_CLIENT_SECRET not configured")
    return client_id, client_secret


class TdxBikeProvider:
    """Talks to TDX Bike v2 for one city.

    Credentials are resolved lazily (on first request, not at construction)
    so the module-level default instance in `services.moovo` can exist even
    before env vars are loaded; a missing/invalid config only surfaces as
    `MoovoConfigError` once a request is actually made.
    """

    def __init__(
        self,
        *,
        base_url: str = _DEFAULT_BIKE_BASE_URL,
        city: str = _DEFAULT_CITY,
        timeout: float = _REQUEST_TIMEOUT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._base = base_url
        self._city = city
        self._timeout = timeout
        self._clock = clock
        self._token_client: TdxTokenClient | None = None
        self._token_client_lock = asyncio.Lock()

    async def fetch_station_payloads(self) -> tuple[list[Any], list[Any]]:
        """Return raw (stations, availability) lists for the configured city."""
        params = {"$format": "JSON"}
        stations, availability = await asyncio.gather(
            self._get_json(f"Station/City/{self._city}", params=params),
            self._get_json(f"Availability/City/{self._city}", params=params),
        )

        if not isinstance(stations, list) or not isinstance(availability, list):
            raise MoovoApiError("TDX Bike station response is not a list")
        return stations, availability

    def reset_token_cache(self) -> None:
        """Forget the cached OAuth token (tests, manual recovery)."""
        if self._token_client is not None:
            self._token_client.invalidate()

    # ── internal ──────────────────────────────────────────────────────────────

    async def _ensure_token_client(self) -> TdxTokenClient:
        if self._token_client is not None:
            return self._token_client
        async with self._token_client_lock:
            # Re-check after acquiring: first caller constructs it, subsequent
            # callers reuse it — otherwise two concurrent cold-start requests
            # (Station + Availability gather) would each fetch their own token.
            if self._token_client is None:
                client_id, client_secret = _tdx_credentials()
                self._token_client = TdxTokenClient(
                    client_id,
                    client_secret,
                    clock=self._clock,
                    timeout=self._timeout,
                    http_client_factory=get_http_client,
                    record_hit=lambda hit: get_telemetry().record_cache_lookup(cache="tdx.token", hit=hit),
                )
            return self._token_client

    async def _get_json(self, path: str, *, params: dict[str, str] | None = None) -> Any:
        token_client = await self._ensure_token_client()
        client = get_http_client()

        async def _do(token: str) -> httpx.Response:
            return await client.get(
                f"{self._base}/{path}",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                },
                params=params,
                timeout=self._timeout,
            )

        try:
            response = await token_client.request_with_retry(_do)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as error:
            raise MoovoApiError(f"TDX Bike request failed: {error}") from error
        except ValueError as error:
            raise MoovoApiError("TDX Bike response is not valid JSON") from error
