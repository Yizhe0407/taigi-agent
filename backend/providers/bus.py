"""Provider-neutral bus data contracts.

Concrete upstream clients translate their native payloads into these models before
anything leaves ``providers``.  The services layer must not know whether data came
from TDX, Ebus, TaiwanBus, or a test double, and never sees an upstream field name
or status code — adapters are responsible for producing fully typed rows.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum, StrEnum
from typing import Protocol


class BusProviderError(RuntimeError):
    """Base error raised when a bus provider cannot return usable data."""


class BusProviderConfigError(BusProviderError):
    """Raised when a provider chain is not configured for use."""


class Direction(IntEnum):
    """Provider-neutral route direction."""

    OUTBOUND = 0
    INBOUND = 1


class StopStatus(StrEnum):
    """Provider-neutral status of a route at a stop."""

    AVAILABLE = "available"
    NOT_DEPARTED = "not_departed"
    NOT_STOPPING = "not_stopping"
    LAST_DEPARTED = "last_departed"
    NOT_OPERATING = "not_operating"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class RouteInfo:
    """A route serving a stop and its two terminal labels."""

    route_name: str
    outbound_destination: str = ""
    inbound_destination: str = ""


@dataclass(frozen=True, slots=True)
class RouteAtStop:
    """A route/direction pair serving a stop."""

    route_name: str
    direction: Direction


@dataclass(frozen=True, slots=True)
class StopArrival:
    """The next vehicle status for a route at one stop."""

    route_name: str
    direction: Direction
    status: StopStatus
    eta_seconds: int | None = None
    sequence: int | None = None
    scheduled_time: str | None = None
    vehicle_id: str | None = None


@dataclass(frozen=True, slots=True)
class RouteStopEstimate:
    """A normalized route stop row used for route details and destination ETA."""

    stop_name: str
    sequence: int | None
    direction: Direction
    status: StopStatus
    eta_seconds: int | None = None
    scheduled_time: str | None = None
    vehicle_id: str | None = None
    route_name: str | None = None


class BusProvider(Protocol):
    """Provider-neutral read-only view of a bus network."""

    async def fetch_routes_at_stop(self, stop_name: str) -> list[RouteAtStop]:
        """Return routes serving ``stop_name``."""
        ...

    async def fetch_eta_at_stop(self, stop_name: str) -> list[StopArrival] | None:
        """Return arrivals; ``None`` means the provider was unavailable."""
        ...

    async def fetch_route_estimate(self, route_name: str) -> list[RouteStopEstimate] | None:
        """Return the ordered stops for ``route_name``; ``None`` means unsupported."""
        ...

    async def load_route_info(self, stop_name: str) -> dict[str, RouteInfo]:
        """Return route metadata keyed by route name."""
        ...
