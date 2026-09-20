"""Provider-neutral public bike station service.

This module owns cache and spatial-query policy only.  Provider composition and
upstream protocol details live behind ``services.bike_provider`` and the
``providers.bike`` contract.
"""

from __future__ import annotations

import math
import os
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass

from async_lifecycle import ReclaimingAsyncLock
from providers.bike import (
    BikeProvider,
    BikeProviderApiError,
    BikeProviderConfigError,
    BikeProviderError,
    BikeStation,
)
from services.bike_provider import (
    configure_providers as _configure_providers,
)
from services.bike_provider import (
    get_provider as _get_provider,
)
from services.bike_provider import (
    register_provider as _register_provider,
)
from services.bike_provider import (
    reset_provider as _reset_provider,
)
from services.bike_provider import (
    set_provider as _set_provider,
)
from telemetry import get_telemetry

# Re-exported so API routes and tests can name the domain contract without
# importing a provider module directly.
__all__ = [
    "BikeProvider",
    "BikeProviderApiError",
    "BikeProviderConfigError",
    "BikeProviderError",
    "BikeStation",
    "NearbyBikeStation",
    "clear_station_cache",
    "configure_providers",
    "get_provider",
    "load_bike_stations",
    "nearby_bike_stations",
    "provider_override",
    "register_provider",
    "reset_provider",
    "set_provider",
]

_DEFAULT_CACHE_TTL_SECONDS = 60
_DEFAULT_RADIUS_METERS = 1000
_DEFAULT_LIMIT = 20
_MAX_RADIUS_METERS = 5000


@dataclass(frozen=True, slots=True)
class NearbyBikeStation:
    """A normalized station plus its distance from a requested coordinate."""

    station: BikeStation
    distance_meters: float


_provider_cache: tuple[float, tuple[BikeStation, ...]] | None = None
_stations_lock = ReclaimingAsyncLock("bike station refresh")


def get_provider() -> BikeProvider:
    return _get_provider()


def set_provider(provider: BikeProvider) -> None:
    """Install one provider or an already-composed provider chain."""
    _set_provider(provider)
    clear_station_cache()


@contextmanager
def provider_override(provider: BikeProvider) -> Iterator[BikeProvider]:
    previous = get_provider()
    set_provider(provider)
    try:
        yield provider
    finally:
        set_provider(previous)


def register_provider(name: str, factory) -> None:
    """Register a provider factory in the composition root."""
    _register_provider(name, factory)


def configure_providers(names: Sequence[str]) -> BikeProvider:
    """Select and compose providers in the requested priority order."""
    provider = _configure_providers(names)
    clear_station_cache()
    return provider


def reset_provider() -> None:
    """Reset provider selection so the next read rebuilds configured defaults."""
    _reset_provider()
    clear_station_cache()


def clear_station_cache() -> None:
    """Clear the normalized station cache after a provider switch or test."""
    global _provider_cache
    _provider_cache = None


def _cache_ttl_seconds() -> int:
    try:
        value = int(os.getenv("BIKE_CACHE_TTL_SECONDS", ""))
    except ValueError:
        return _DEFAULT_CACHE_TTL_SECONDS
    return max(0, value)


async def load_bike_stations(*, force_refresh: bool = False) -> tuple[BikeStation, ...]:
    """Load normalized stations without exposing any provider payload format."""
    global _provider_cache

    now = time.monotonic()
    ttl = _cache_ttl_seconds()
    if not force_refresh and _provider_cache is not None:
        fetched_at, stations = _provider_cache
        if ttl > 0 and now - fetched_at < ttl:
            get_telemetry().record_cache_lookup(cache="bike.stations", hit=True)
            return stations

    async with _stations_lock.acquire():
        if not force_refresh and _provider_cache is not None:
            fetched_at, stations = _provider_cache
            if ttl > 0 and time.monotonic() - fetched_at < ttl:
                get_telemetry().record_cache_lookup(cache="bike.stations", hit=True)
                return stations

        get_telemetry().record_cache_lookup(cache="bike.stations", hit=False)
        stations = tuple(await get_provider().fetch_stations())
        if not stations:
            # A composed chain already skips empty providers; this is the
            # single-provider backstop, so an empty feed never gets cached.
            raise BikeProviderApiError("bike provider returned no stations")
        _provider_cache = (time.monotonic(), stations)
        return stations


def _validate_coordinate(latitude: float, longitude: float) -> None:
    if not math.isfinite(latitude) or not math.isfinite(longitude) or not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
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
    a = math.sin(delta_phi / 2) ** 2 + math.cos(origin_phi) * math.cos(target_phi) * math.sin(delta_lambda / 2) ** 2
    return radius * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


async def nearby_bike_stations(
    latitude: float,
    longitude: float,
    *,
    radius_meters: int = _DEFAULT_RADIUS_METERS,
    limit: int = _DEFAULT_LIMIT,
) -> tuple[NearbyBikeStation, ...]:
    """Return stations near a coordinate, sorted by distance."""
    _validate_coordinate(latitude, longitude)
    radius_meters = max(1, min(radius_meters, _MAX_RADIUS_METERS))
    limit = max(1, min(limit, _DEFAULT_LIMIT))

    nearby: list[NearbyBikeStation] = []
    for station in await load_bike_stations():
        distance = _distance_meters(
            latitude,
            longitude,
            station.latitude,
            station.longitude,
        )
        if distance <= radius_meters:
            nearby.append(NearbyBikeStation(station, distance))

    nearby.sort(key=lambda item: item.distance_meters)
    return tuple(nearby[:limit])
