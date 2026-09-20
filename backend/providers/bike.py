"""Provider-neutral public bike-share contracts and value objects.

Concrete upstream clients must translate their native responses into these
objects before the data crosses the provider boundary.  Domain services and
HTTP routes therefore do not need to know whether a station came from TDX,
the operator website, or a future provider.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


class BikeProviderError(RuntimeError):
    """Base error raised when a bike provider cannot return usable data."""


class BikeProviderConfigError(BikeProviderError):
    """Raised when a provider is not configured for use."""


class BikeProviderApiError(BikeProviderError):
    """Raised when an upstream provider returns unusable data or fails."""


@dataclass(frozen=True, slots=True)
class BikeStation:
    """Provider-neutral station snapshot.

    Some providers expose only station coordinates and the current rentable
    count.  Fields they cannot authoritatively provide stay ``None`` rather
    than being converted into a misleading zero.
    """

    station_uid: str
    station_id: str | None
    name: str
    latitude: float
    longitude: float
    bike_capacity: int | None
    available_rent_bikes: int | None
    available_return_bikes: int | None
    service_status: int | None
    update_time: datetime | None
    provider: str = "unknown"


class BikeProvider(Protocol):
    """Read-only source of normalized public bike-share stations."""

    async def fetch_stations(self) -> tuple[BikeStation, ...]:
        """Return one normalized station snapshot."""
        ...
