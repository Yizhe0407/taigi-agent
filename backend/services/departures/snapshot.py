from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime

from services.departures.classification import (
    DepartureDecision,
    DepartureSection,
    StopClassification,
    _classify_stop,
)
from services.departures.normalize import (
    TAIPEI_TZ,
    _as_int,
    _direction_label_from_info,
    _is_terminal_direction,
)
from services.departures.provider import get_provider

_log = logging.getLogger(__name__)


class DepartureSnapshotUnavailable(RuntimeError):
    """Raised when the upstream source cannot provide departure data."""


class RouteDetailNotFound(RuntimeError):
    """Raised when the configured kiosk stop does not serve a route."""


class RouteDetailUnavailable(RuntimeError):
    """Raised when the upstream source cannot provide route details."""


@dataclass(frozen=True)
class DepartureRouteStatus:
    id: str
    route: str
    route_id: int
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
    route_id: int
    stop_name: str
    direction_filter: int | None
    directions: tuple[RouteDirectionDetail, ...]


def _summary(routes: tuple[DepartureRouteStatus, ...]) -> DepartureSummary:
    return DepartureSummary(
        available_count=sum(
            1 for r in routes if r.section == DepartureSection.AVAILABLE
        ),
        not_departed_count=sum(
            1 for r in routes if r.section == DepartureSection.NOT_DEPARTED
        ),
        last_departed_count=sum(
            1 for r in routes if r.section == DepartureSection.LAST_DEPARTED
        ),
        unknown_count=sum(
            1 for r in routes if r.section == DepartureSection.UNKNOWN
        ),
    )


def _sort_key(route: DepartureRouteStatus) -> tuple[int, int, str, int]:
    return (route.sort_priority, route.sort_minutes, route.route, route.go_back)


def _stop_detail_from_row(stop: dict, kiosk_stop_name: str) -> RouteStopDetail | None:
    seq = _as_int(stop.get("SeqNo"))
    name = str(stop.get("StopName") or "").strip()
    if seq is None or not name:
        return None

    c = _classify_stop(stop, datetime.now(TAIPEI_TZ))
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
        raise DepartureSnapshotUnavailable(
            "公車資訊暫時無法取得，請稍後再試"
        ) from error

    route_by_id = {info["id"]: name for name, info in route_info.items()}
    routes: list[DepartureRouteStatus] = []
    seen: set[tuple[str, int, str]] = set()

    for stop in eta_data:
        stop_go_back = _as_int(stop.get("GoBack")) or 1

        route_id = _as_int(stop.get("xno"))
        if route_id is None:
            continue

        route = route_by_id.get(route_id)
        if route is None:
            continue

        if go_back is not None:
            if stop_go_back != go_back:
                continue
        else:
            if _is_terminal_direction(stop_name, route_info, route, stop_go_back):
                continue

        c = _classify_stop(stop, now)
        direction = _direction_label_from_info(route_info, route, stop_go_back)
        dedupe_key = (route, stop_go_back, c.status_text)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)

        routes.append(
            DepartureRouteStatus(
                id=f"{route_id}-{stop_go_back}",
                route=route,
                route_id=route_id,
                direction=direction,
                go_back=stop_go_back,
                section=c.section,
                decision=c.decision,
                status_text=c.status_text,
                decision_text=c.decision_text,
                minutes=c.minutes,
                scheduled_time=c.scheduled_time,
                sort_priority=c.sort_priority,
                sort_minutes=c.sort_minutes,
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
        raise RouteDetailUnavailable("路線詳情暫時無法取得，請稍後再試") from error

    info = route_info.get(route)
    if info is None:
        raise RouteDetailNotFound(f"在 {stop_name} 找不到停靠路線 {route}")

    route_id = _as_int(info.get("id"))
    if route_id is None:
        raise RouteDetailUnavailable(f"路線 {route} 的 route id 格式異常")

    try:
        estimate_data = await provider.fetch_route_estimate(route_id)
    except Exception as error:
        _log.warning("Route detail fetch (fetch_route_estimate) failed: %s", error)
        raise RouteDetailUnavailable("路線詳情暫時無法取得，請稍後再試") from error

    by_direction: dict[int, list[RouteStopDetail]] = {}
    for row in estimate_data:
        row_go_back = _as_int(row.get("GoBack")) or 1
        if go_back is not None and row_go_back != go_back:
            continue

        stop_detail = _stop_detail_from_row(row, stop_name)
        if stop_detail is None:
            continue
        by_direction.setdefault(row_go_back, []).append(stop_detail)

    directions = tuple(
        RouteDirectionDetail(
            go_back=row_go_back,
            label=_direction_label_from_info(route_info, route, row_go_back),
            stops=tuple(sorted(stops, key=lambda s: s.seq)),
        )
        for row_go_back, stops in sorted(by_direction.items())
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
