"""TDX Bike v2 provider.

This module owns TDX authentication, HTTP details, and TDX-to-neutral
normalization.  Nothing outside this adapter should depend on TDX payload
field names or OAuth semantics.
"""

from __future__ import annotations

import asyncio
import math
import os
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

import httpx

from async_lifecycle import ReclaimingAsyncLock
from providers.bike import BikeProviderApiError, BikeProviderConfigError, BikeStation
from providers.http import get_http_client
from providers.tdx_auth import TdxTokenClient
from telemetry import get_telemetry

_DEFAULT_BIKE_BASE_URL = "https://tdx.transportdata.tw/api/basic/v2/Bike"
_DEFAULT_CITY = "YunlinCounty"
_REQUEST_TIMEOUT_SECONDS = 20.0


def _tdx_credentials() -> tuple[str, str]:
    client_id = os.getenv("TDX_CLIENT_ID")
    client_secret = os.getenv("TDX_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise BikeProviderConfigError("TDX_CLIENT_ID / TDX_CLIENT_SECRET not configured")
    return client_id, client_secret


def _parse_float(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _parse_non_negative_int(value: object) -> int | None:
    """Return a non-negative count, or ``None`` when TDX did not report one.

    A missing counter is not a zero.  The neutral contract asks providers to
    leave unknown fields absent instead of claiming an empty station, which the
    UI would otherwise paint as "no bikes available".
    """
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return max(0, number)


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _zh_name(data: dict[str, Any], field: str) -> str | None:
    value = data.get(field)
    if not isinstance(value, dict):
        return None
    name = value.get("Zh_tw") or value.get("Zh")
    return name.strip() if isinstance(name, str) and name.strip() else None


def _availability_by_station_uid(payload: list[Any]) -> dict[str, dict[str, Any]]:
    availability: dict[str, dict[str, Any]] = {}
    for item in payload:
        if not isinstance(item, dict):
            continue
        station_uid = item.get("StationUID")
        if isinstance(station_uid, str) and station_uid:
            availability[station_uid] = item
    return availability


def _parse_station(data: dict[str, Any], availability: dict[str, Any] | None) -> BikeStation | None:
    station_uid = data.get("StationUID")
    position = data.get("StationPosition")
    name = _zh_name(data, "StationName")
    if not isinstance(station_uid, str) or not station_uid or not isinstance(position, dict):
        return None

    latitude = _parse_float(position.get("PositionLat"))
    longitude = _parse_float(position.get("PositionLon"))
    if latitude is None or longitude is None or name is None:
        return None

    availability = availability or {}
    station_id = data.get("StationID")
    service_status = availability.get("ServiceStatus", data.get("ServiceStatus"))
    update_time = availability.get("UpdateTime") or availability.get("SrcUpdateTime")
    return BikeStation(
        station_uid=station_uid,
        station_id=station_id if isinstance(station_id, str) else None,
        name=name,
        latitude=latitude,
        longitude=longitude,
        bike_capacity=_parse_non_negative_int(data.get("BikesCapacity")),
        available_rent_bikes=_parse_non_negative_int(availability.get("AvailableRentBikes")),
        available_return_bikes=_parse_non_negative_int(availability.get("AvailableReturnBikes")),
        service_status=_parse_non_negative_int(service_status),
        update_time=_parse_datetime(update_time),
        provider="tdx",
    )


def _merge_station_payloads(stations_payload: list[Any], availability_payload: list[Any]) -> tuple[BikeStation, ...]:
    availability = _availability_by_station_uid(availability_payload)
    stations: list[BikeStation] = []
    seen: set[str] = set()
    for item in stations_payload:
        if not isinstance(item, dict):
            continue
        station = _parse_station(item, availability.get(str(item.get("StationUID"))))
        if station is None or station.station_uid in seen:
            continue
        seen.add(station.station_uid)
        stations.append(station)
    stations.sort(key=lambda station: (station.name, station.station_uid))
    return tuple(stations)


class TdxBikeProvider:
    """Fetch and normalize one city's TDX public-bike feed."""

    name = "tdx"

    def __init__(
        self,
        *,
        base_url: str = _DEFAULT_BIKE_BASE_URL,
        city: str = _DEFAULT_CITY,
        timeout: float = _REQUEST_TIMEOUT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._city = city
        self._timeout = timeout
        self._clock = clock
        self._token_client: TdxTokenClient | None = None
        self._token_client_lock = ReclaimingAsyncLock("TDX bike token-client construction")

    async def fetch_stations(self) -> tuple[BikeStation, ...]:
        params = {"$format": "JSON"}
        stations, availability = await asyncio.gather(
            self._get_json(f"Station/City/{self._city}", params=params),
            self._get_json(f"Availability/City/{self._city}", params=params),
        )
        if not isinstance(stations, list) or not isinstance(availability, list):
            raise BikeProviderApiError("TDX Bike station response is not a list")
        return _merge_station_payloads(stations, availability)

    def reset_token_cache(self) -> None:
        """Forget the cached OAuth token (tests, manual recovery)."""
        if self._token_client is not None:
            self._token_client.invalidate()

    async def _ensure_token_client(self) -> TdxTokenClient:
        if self._token_client is not None:
            return self._token_client
        async with self._token_client_lock.acquire():
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
            raise BikeProviderApiError(f"TDX Bike request failed: {error}") from error
        except ValueError as error:
            raise BikeProviderApiError("TDX Bike response is not valid JSON") from error
