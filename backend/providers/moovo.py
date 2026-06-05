"""TDX Bike v2 provider — HTTP fetch + OAuth token cache.

Only knows about TDX endpoints / token semantics. Parsing, dataclasses,
caching policy, and spatial queries live in `services.moovo` so domain
logic stays decoupled from transport.
"""

from __future__ import annotations

import os
import time
from typing import Any

import httpx

_TOKEN_URL = "https://tdx.transportdata.tw/auth/realms/TDXConnect/protocol/openid-connect/token"
_DEFAULT_BIKE_BASE_URL = "https://tdx.transportdata.tw/api/basic/v2/Bike"
_DEFAULT_CITY = "YunlinCounty"
_REQUEST_TIMEOUT_SECONDS = 20.0


class MoovoError(RuntimeError):
    """Raised when MOOVO bike-station data cannot be returned."""


class MoovoConfigError(MoovoError):
    """Raised when TDX credentials are not configured."""


class MoovoApiError(MoovoError):
    """Raised when TDX Bike API returns unusable data."""


def _tdx_credentials() -> tuple[str, str]:
    client_id = os.getenv("TDX_CLIENT_ID")
    client_secret = os.getenv("TDX_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise MoovoConfigError("TDX_CLIENT_ID / TDX_CLIENT_SECRET not configured")
    return client_id, client_secret


class TdxBikeProvider:
    """Talks to TDX Bike v2 for one city.

    The OAuth access token is memoised on the instance — fresh providers
    re-authenticate, which keeps tests deterministic.
    """

    def __init__(
        self,
        *,
        base_url: str = _DEFAULT_BIKE_BASE_URL,
        city: str = _DEFAULT_CITY,
        timeout: float = _REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        self._base = base_url
        self._city = city
        self._timeout = timeout
        self._token_cache: tuple[float, str] | None = None

    async def fetch_station_payloads(self) -> tuple[list[Any], list[Any]]:
        """Return raw (stations, availability) lists for the configured city."""
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            token = await self._get_token(client)
            params = {"$format": "JSON"}
            stations = await self._get_json(
                client,
                token,
                f"Station/City/{self._city}",
                params=params,
            )
            availability = await self._get_json(
                client,
                token,
                f"Availability/City/{self._city}",
                params=params,
            )

        if not isinstance(stations, list) or not isinstance(availability, list):
            raise MoovoApiError("TDX Bike station response is not a list")
        return stations, availability

    def reset_token_cache(self) -> None:
        """Forget the cached OAuth token (tests, manual recovery)."""
        self._token_cache = None

    # ── internal ──────────────────────────────────────────────────────────────

    async def _get_token(self, client: httpx.AsyncClient) -> str:
        now = time.monotonic()
        if self._token_cache is not None:
            expires_at, token = self._token_cache
            if now < expires_at:
                return token

        client_id, client_secret = _tdx_credentials()
        try:
            response = await client.post(
                _TOKEN_URL,
                data={
                    "grant_type": "client_credentials",
                    "client_id": client_id,
                    "client_secret": client_secret,
                },
            )
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPError as error:
            raise MoovoApiError(f"TDX token request failed: {error}") from error
        except ValueError as error:
            raise MoovoApiError("TDX token response is not valid JSON") from error

        token = payload.get("access_token")
        if not isinstance(token, str) or not token:
            raise MoovoApiError("TDX token response has no access_token")

        expires_in = payload.get("expires_in")
        ttl = int(expires_in) if isinstance(expires_in, int | float) else 3600
        self._token_cache = (now + max(60, ttl - 60), token)
        return token

    async def _get_json(
        self,
        client: httpx.AsyncClient,
        token: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
    ) -> Any:
        try:
            response = await client.get(
                f"{self._base}/{path}",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                },
                params=params,
            )
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as error:
            raise MoovoApiError(f"TDX Bike request failed: {error}") from error
        except ValueError as error:
            raise MoovoApiError("TDX Bike response is not valid JSON") from error
