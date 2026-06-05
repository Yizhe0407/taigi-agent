"""Departure decisions for the kiosk stop — single classification source.

This package owns the rules that turn a raw bus ETA row into a user-facing
decision. Both the HTTP API (structured dataclasses) and the LLM agent
(string renderers) call into the same `_classify_stop` so their wording
cannot drift.

Provider I/O is reached through the `BusProvider` Protocol; the active
instance lives at module scope (`_provider`) and can be swapped via
`set_provider()` — tests inject a fake, production wires the concrete
`YunlinEbusProvider`.
"""

from services.departures.classification import (
    DepartureDecision,
    DepartureSection,
    StopClassification,
    _classify_stop,
)
from services.departures.normalize import (
    TAIPEI_TZ,
    _as_int,
)
from services.departures.provider import (
    get_provider,
    provider_override,
    set_provider,
)
from services.departures.renderers import (
    render_arrivals,
    render_arrivals_to_destination,
    render_route_stops,
    render_routes_at_stop,
    render_stop_arrival_statuses,
    render_stop_on_route,
)
from services.departures.snapshot import (
    DepartureRouteDetail,
    DepartureRouteStatus,
    DepartureSnapshotUnavailable,
    DepartureSummary,
    RouteDetailNotFound,
    RouteDetailUnavailable,
    RouteDirectionDetail,
    RouteStopDetail,
    StopDepartureSnapshot,
    build_departure_snapshot,
    build_route_detail,
)

__all__ = [
    # enums
    "DepartureSection",
    "DepartureDecision",
    # dataclasses
    "StopClassification",
    "DepartureRouteStatus",
    "DepartureSummary",
    "StopDepartureSnapshot",
    "RouteStopDetail",
    "RouteDirectionDetail",
    "DepartureRouteDetail",
    # exceptions
    "DepartureSnapshotUnavailable",
    "RouteDetailNotFound",
    "RouteDetailUnavailable",
    # provider
    "get_provider",
    "set_provider",
    "provider_override",
    # builders (HTTP API)
    "build_departure_snapshot",
    "build_route_detail",
    # renderers (LLM agent)
    "render_arrivals",
    "render_stop_arrival_statuses",
    "render_route_stops",
    "render_stop_on_route",
    "render_arrivals_to_destination",
    "render_routes_at_stop",
    # constants & internals (used by tests / tools)
    "TAIPEI_TZ",
    "_classify_stop",
    "_as_int",
]
