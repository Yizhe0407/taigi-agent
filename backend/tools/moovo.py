"""MOOVO public bike station provider backed by TDX Bike v2."""

from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import requests

_TOKEN_URL = (
    "https://tdx.transportdata.tw/auth/realms/TDXConnect/"
    "protocol/openid-connect/token"
)
_TDX_BIKE_BASE_URL = "https://tdx.transportdata.tw/api/basic/v2/Bike"
_YUNLIN_CITY = "YunlinCounty"
_DEFAULT_CACHE_TTL_SECONDS = 60
_DEFAULT_RADIUS_METERS = 1000
_DEFAULT_LIMIT = 20
_MAX_RADIUS_METERS = 5000
_REQUEST_TIMEOUT_SECONDS = 20


class MoovoError(RuntimeError):
    """Raised when MOOVO station data cannot be returned."""


class MoovoConfigError(MoovoError):
    """Raised when TDX credentials are not configured."""


class MoovoApiError(MoovoError):
    """Raised when TDX Bike API returns unusable data."""


@dataclass(frozen=True)
class MoovoStation:
    station_uid: str
    station_id: str | None
    name: str
    latitude: float
    longitude: float
    bike_capacity: int
    available_rent_bikes: int
    available_return_bikes: int
    service_status: int
    update_time: datetime | None


@dataclass(frozen=True)
class NearbyMoovoStation:
    station: MoovoStation
    distance_meters: float


_stations_cache: tuple[float, tuple[MoovoStation, ...]] | None = None
_token_cache: tuple[float, str] | None = None


def _cache_ttl_seconds() -> int:
    try:
        value = int(os.getenv("MOOVO_CACHE_TTL_SECONDS", ""))
    except ValueError:
        return _DEFAULT_CACHE_TTL_SECONDS
    return max(0, value)


def _tdx_credentials() -> tuple[str, str]:
    client_id = os.getenv("TDX_CLIENT_ID")
    client_secret = os.getenv("TDX_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise MoovoConfigError("TDX_CLIENT_ID / TDX_CLIENT_SECRET not configured")
    return client_id, client_secret


def _get_tdx_token(session: requests.Session) -> str:
    global _token_cache

    now = time.monotonic()
    if _token_cache is not None:
        expires_at, token = _token_cache
        if now < expires_at:
            return token

    client_id, client_secret = _tdx_credentials()
    try:
        response = session.post(
            _TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
            },
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as error:
        raise MoovoApiError(f"TDX token request failed: {error}") from error
    except ValueError as error:
        raise MoovoApiError("TDX token response is not valid JSON") from error

    token = payload.get("access_token")
    if not isinstance(token, str) or not token:
        raise MoovoApiError("TDX token response has no access_token")

    expires_in = payload.get("expires_in")
    ttl = int(expires_in) if isinstance(expires_in, int | float) else 3600
    _token_cache = (now + max(60, ttl - 60), token)
    return token


def _tdx_get_json(
    session: requests.Session,
    token: str,
    path: str,
    *,
    params: dict[str, str] | None = None,
) -> Any:
    try:
        response = session.get(
            f"{_TDX_BIKE_BASE_URL}/{path}",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            params=params,
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        return response.json()
    except requests.RequestException as error:
        raise MoovoApiError(f"TDX Bike request failed: {error}") from error
    except ValueError as error:
        raise MoovoApiError("TDX Bike response is not valid JSON") from error


def _fetch_tdx_payloads() -> tuple[list[Any], list[Any]]:
    with requests.Session() as session:
        token = _get_tdx_token(session)
        params = {"$format": "JSON"}
        stations = _tdx_get_json(
            session,
            token,
            f"Station/City/{_YUNLIN_CITY}",
            params=params,
        )
        availability = _tdx_get_json(
            session,
            token,
            f"Availability/City/{_YUNLIN_CITY}",
            params=params,
        )

    if not isinstance(stations, list) or not isinstance(availability, list):
        raise MoovoApiError("TDX Bike station response is not a list")
    return stations, availability


def _zh_name(data: dict[str, Any], field: str) -> str | None:
    value = data.get(field)
    if not isinstance(value, dict):
        return None
    name = value.get("Zh_tw") or value.get("Zh")
    return name.strip() if isinstance(name, str) and name.strip() else None


def _parse_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _parse_non_negative_int(value: Any, *, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(0, number)


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _availability_by_station_uid(payload: list[Any]) -> dict[str, dict[str, Any]]:
    availability: dict[str, dict[str, Any]] = {}
    for item in payload:
        if not isinstance(item, dict):
            continue
        station_uid = item.get("StationUID")
        if isinstance(station_uid, str) and station_uid:
            availability[station_uid] = item
    return availability


def _parse_station(
    data: dict[str, Any],
    availability: dict[str, Any] | None,
) -> MoovoStation | None:
    station_uid = data.get("StationUID")
    position = data.get("StationPosition")
    name = _zh_name(data, "StationName")
    if not isinstance(station_uid, str) or not station_uid or not isinstance(
        position, dict
    ):
        return None

    latitude = _parse_float(position.get("PositionLat"))
    longitude = _parse_float(position.get("PositionLon"))
    if latitude is None or longitude is None or name is None:
        return None

    availability = availability or {}
    station_id = data.get("StationID")
    service_status = availability.get("ServiceStatus", data.get("ServiceStatus", 0))
    update_time = availability.get("UpdateTime") or availability.get("SrcUpdateTime")
    return MoovoStation(
        station_uid=station_uid,
        station_id=station_id if isinstance(station_id, str) else None,
        name=name,
        latitude=latitude,
        longitude=longitude,
        bike_capacity=_parse_non_negative_int(data.get("BikesCapacity")),
        available_rent_bikes=_parse_non_negative_int(
            availability.get("AvailableRentBikes")
        ),
        available_return_bikes=_parse_non_negative_int(
            availability.get("AvailableReturnBikes")
        ),
        service_status=_parse_non_negative_int(service_status),
        update_time=_parse_datetime(update_time),
    )


def _merge_station_payloads(
    stations_payload: list[Any],
    availability_payload: list[Any],
) -> tuple[MoovoStation, ...]:
    availability = _availability_by_station_uid(availability_payload)
    stations: list[MoovoStation] = []
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


def clear_moovo_cache() -> None:
    """Clear in-memory MOOVO caches. Intended for tests and manual refreshes."""
    global _stations_cache, _token_cache
    _stations_cache = None
    _token_cache = None


def load_moovo_stations(*, force_refresh: bool = False) -> tuple[MoovoStation, ...]:
    """Load Yunlin MOOVO stations from TDX Bike v2 with a short TTL cache."""
    global _stations_cache

    now = time.monotonic()
    ttl = _cache_ttl_seconds()
    if not force_refresh and _stations_cache is not None:
        fetched_at, stations = _stations_cache
        if ttl > 0 and now - fetched_at < ttl:
            return stations

    stations_payload, availability_payload = _fetch_tdx_payloads()
    stations = _merge_station_payloads(stations_payload, availability_payload)
    if not stations:
        raise MoovoApiError("TDX Bike Yunlin station response is empty")
    _stations_cache = (now, stations)
    return stations


def _validate_coordinate(latitude: float, longitude: float) -> None:
    if (
        not math.isfinite(latitude)
        or not math.isfinite(longitude)
        or not -90 <= latitude <= 90
        or not -180 <= longitude <= 180
    ):
        raise ValueError("invalid coordinate")


def _distance_meters(
    origin_latitude: float,
    origin_longitude: float,
    target_latitude: float,
    target_longitude: float,
) -> float:
    radius = 6_371_000
    origin_phi = math.radians(origin_latitude)
    target_phi = math.radians(target_latitude)
    delta_phi = math.radians(target_latitude - origin_latitude)
    delta_lambda = math.radians(target_longitude - origin_longitude)
    a = (
        math.sin(delta_phi / 2) ** 2
        + math.cos(origin_phi)
        * math.cos(target_phi)
        * math.sin(delta_lambda / 2) ** 2
    )
    return radius * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def nearby_moovo_stations(
    latitude: float,
    longitude: float,
    *,
    radius_meters: int = _DEFAULT_RADIUS_METERS,
    limit: int = _DEFAULT_LIMIT,
) -> tuple[NearbyMoovoStation, ...]:
    """Return Yunlin MOOVO stations near a coordinate, sorted by distance."""
    _validate_coordinate(latitude, longitude)
    radius_meters = max(1, min(radius_meters, _MAX_RADIUS_METERS))
    limit = max(1, min(limit, _DEFAULT_LIMIT))

    nearby: list[NearbyMoovoStation] = []
    for station in load_moovo_stations():
        distance = _distance_meters(
            latitude,
            longitude,
            station.latitude,
            station.longitude,
        )
        if distance <= radius_meters:
            nearby.append(NearbyMoovoStation(station, distance))

    nearby.sort(key=lambda item: item.distance_meters)
    return tuple(nearby[:limit])
