from __future__ import annotations

import asyncio
import logging
from collections import Counter
from dataclasses import dataclass
from datetime import datetime

from services.departures.classification import (
    DepartureDecision,
    DepartureSection,
    _classify_stop,
)
from services.departures.normalize import (
    TAIPEI_TZ,
    _direction_label_from_info,
    iter_scoped_stop_etas,
)
from services.departures.provider import get_provider
from telemetry import get_telemetry

_log = logging.getLogger(__name__)


class DepartureSnapshotUnavailable(RuntimeError):
    """Raised when the upstream source cannot provide departure data."""


class RouteDetailNotFound(RuntimeError):
    """Raised when the configured kiosk stop does not serve a route."""


class RouteDetailUnavailable(RuntimeError):
    """Raised when the upstream source cannot provide route details."""


_ROUTE_DETAIL_UNAVAILABLE = "路線詳情暫時無法取得，請稍後再試"


@dataclass(frozen=True)
class DepartureRouteStatus:
    id: str
    route: str
    route_id: str
    direction: str
    go_back: int
    section: DepartureSection
    decision: DepartureDecision
    status_text: str
    decision_text: str
    minutes: int | None
    scheduled_time: str | None
    sort_priority: int
    sort_minutes: int  # minutes-from-now for sorting; not exposed in API response
    car_id: str | None


@dataclass(frozen=True)
class DepartureSummary:
    available_count: int
    not_departed_count: int
    last_departed_count: int
    unknown_count: int


@dataclass(frozen=True)
class StopDepartureSnapshot:
    stop_name: str
    direction_filter: int | None
    updated_at: datetime
    routes: tuple[DepartureRouteStatus, ...]
    summary: DepartureSummary


@dataclass(frozen=True)
class RouteStopDetail:
    seq: int
    name: str
    is_current_stop: bool
    status_text: str
    minutes: int | None
    scheduled_time: str | None


@dataclass(frozen=True)
class RouteDirectionDetail:
    go_back: int
    label: str
    stops: tuple[RouteStopDetail, ...]


@dataclass(frozen=True)
class DepartureRouteDetail:
    route: str
    route_id: str
    stop_name: str
    direction_filter: int | None
    directions: tuple[RouteDirectionDetail, ...]


def _summary(routes: tuple[DepartureRouteStatus, ...]) -> DepartureSummary:
    counts = Counter(r.section for r in routes)
    return DepartureSummary(
        available_count=counts[DepartureSection.AVAILABLE],
        not_departed_count=counts[DepartureSection.NOT_DEPARTED],
        last_departed_count=counts[DepartureSection.LAST_DEPARTED],
        unknown_count=counts[DepartureSection.UNKNOWN],
    )


def _sort_key(route: DepartureRouteStatus) -> tuple[int, int, str, int]:
    return (route.sort_minutes, route.sort_priority, route.route, route.go_back)


def _stop_detail_from_row(stop: dict, kiosk_stop_name: str, now: datetime) -> RouteStopDetail | None:
    seq = stop.get("stop_sequence")
    name = str(stop.get("stop_name") or "").strip()
    if seq is None or not name:
        return None

    c = _classify_stop(stop, now)
    return RouteStopDetail(
        seq=seq,
        name=name,
        is_current_stop=kiosk_stop_name in name,
        status_text=c.status_text,
        minutes=c.minutes,
        scheduled_time=c.scheduled_time,
    )


async def build_departure_snapshot(
    stop_name: str,
    go_back: int | None = None,
    *,
    updated_at: datetime | None = None,
) -> StopDepartureSnapshot:
    """Build a structured departure snapshot without blocking the event loop."""
    now = datetime.now(TAIPEI_TZ)
    provider = get_provider()

    try:
        eta_data, route_info = await asyncio.gather(
            provider.fetch_eta_at_stop(stop_name),
            provider.load_route_info(stop_name),
        )
    except Exception as error:
        _log.warning("Departure snapshot fetch failed: %s", error)
        raise DepartureSnapshotUnavailable("公車資訊暫時無法取得，請稍後再試") from error

    routes: list[DepartureRouteStatus] = []
    seen: set[tuple[str, int, str]] = set()

    for stop, route, route_id, stop_direction in iter_scoped_stop_etas(eta_data, route_info, stop_name, go_back):
        c = _classify_stop(stop, now)
        get_telemetry().record_departure_decision(decision=c.decision.value)
        direction = _direction_label_from_info(route_info, route, stop_direction)
        dedupe_key = (route, stop_direction, c.status_text)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)

        routes.append(
            DepartureRouteStatus(
                id=f"{route_id}-{stop_direction}",
                route=route,
                route_id=route_id,
                direction=direction,
                go_back=stop_direction,
                section=c.section,
                decision=c.decision,
                status_text=c.status_text,
                decision_text=c.decision_text,
                minutes=c.minutes,
                scheduled_time=c.scheduled_time,
                sort_priority=c.sort_priority,
                sort_minutes=c.sort_minutes,
                car_id=stop.get("car_id") or None,
            )
        )

    ordered = tuple(sorted(routes, key=_sort_key))
    return StopDepartureSnapshot(
        stop_name=stop_name,
        direction_filter=go_back,
        updated_at=updated_at or datetime.now(TAIPEI_TZ),
        routes=ordered,
        summary=_summary(ordered),
    )


async def build_route_detail(
    route: str,
    stop_name: str,
    go_back: int | None = None,
) -> DepartureRouteDetail:
    """Build structured stop-order details without blocking the event loop."""
    provider = get_provider()
    try:
        route_info = await provider.load_route_info(stop_name)
    except Exception as error:
        _log.warning("Route detail fetch (load_route_info) failed: %s", error)
        raise RouteDetailUnavailable(_ROUTE_DETAIL_UNAVAILABLE) from error

    info = route_info.get(route)
    if info is None:
        raise RouteDetailNotFound(f"在 {stop_name} 找不到停靠路線 {route}")

    route_id = info.get("id")
    if not route_id:
        raise RouteDetailUnavailable(f"路線 {route} 的 route id 格式異常")

    try:
        estimate_data = await provider.fetch_route_estimate(route_id)
    except Exception as error:
        _log.warning("Route detail fetch (fetch_route_estimate) failed: %s", error)
        raise RouteDetailUnavailable(_ROUTE_DETAIL_UNAVAILABLE) from error

    now = datetime.now(TAIPEI_TZ)
    by_direction: dict[int, list[RouteStopDetail]] = {}
    for row in estimate_data:
        row_direction = row.get("direction", 0)
        if go_back is not None and row_direction != go_back:
            continue

        stop_detail = _stop_detail_from_row(row, stop_name, now)
        if stop_detail is None:
            continue
        by_direction.setdefault(row_direction, []).append(stop_detail)

    directions = tuple(
        RouteDirectionDetail(
            go_back=row_direction,
            label=_direction_label_from_info(route_info, route, row_direction),
            stops=tuple(sorted(stops, key=lambda s: s.seq)),
        )
        for row_direction, stops in sorted(by_direction.items())
        if stops
    )

    if not directions:
        raise RouteDetailNotFound(f"路線 {route} 無站牌資料")

    return DepartureRouteDetail(
        route=route,
        route_id=route_id,
        stop_name=stop_name,
        direction_filter=go_back,
        directions=directions,
    )
