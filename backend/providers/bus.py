"""Provider-neutral bus data contracts.

Concrete upstream clients translate their native payloads into these models before
anything leaves ``providers``.  The services layer must not know whether data came
from TDX or a test double, and never sees an upstream field name
or status code — adapters are responsible for producing fully typed rows.

ETAs: ``eta_seconds`` is relative to when the upstream estimated it, so it goes
stale the moment a row is cached.  Adapters that know that moment also set
``arrival_at``, the absolute instant the estimate points at; consumers ask a row
``seconds_until_arrival(now)`` instead of reading ``eta_seconds`` directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import IntEnum, StrEnum
from typing import Protocol


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
    """A route serving a stop: its two terminal labels and its static stop order.

    ``outbound_stops`` / ``inbound_stops`` are the route's stop names in travel
    order (ascending stop sequence), one tuple per direction.  They are route
    topology, not live data: the adapter already reads the full ordered stop
    list while discovering which routes serve a stop, so it must keep it here.
    Services answer "does this route reach X after this stop?" from these
    tuples without an upstream round-trip per route.  An empty tuple means the
    source does not know that direction's stops.
    """

    route_name: str
    outbound_destination: str = ""
    inbound_destination: str = ""
    outbound_stops: tuple[str, ...] = ()
    inbound_stops: tuple[str, ...] = ()

    def stops(self, direction: Direction) -> tuple[str, ...]:
        return self.outbound_stops if direction == Direction.OUTBOUND else self.inbound_stops


@dataclass(frozen=True, slots=True)
class RouteAtStop:
    """A route/direction pair serving a stop."""

    route_name: str
    direction: Direction


def _seconds_until(arrival_at: datetime | None, eta_seconds: int | None, now: datetime) -> int | None:
    if arrival_at is None:
        return eta_seconds
    # Whole seconds, rounded: a few ms between the read and `now` must not knock a minute off.
    return round((arrival_at - now).total_seconds())


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
    arrival_at: datetime | None = None

    def seconds_until_arrival(self, now: datetime) -> int | None:
        """Seconds from ``now`` to the estimated arrival (negative once it has passed)."""
        return _seconds_until(self.arrival_at, self.eta_seconds, now)


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
    arrival_at: datetime | None = None

    def seconds_until_arrival(self, now: datetime) -> int | None:
        """Seconds from ``now`` to the estimated arrival (negative once it has passed)."""
        return _seconds_until(self.arrival_at, self.eta_seconds, now)


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
