"""Provider-neutral shaping of route and arrival models."""

from __future__ import annotations

from collections.abc import Iterator, Sequence

from providers.bus import (
    Direction,
    RouteInfo,
    RouteStopEstimate,
    StopArrival,
    StopStatus,
)
from services.departures.normalize import _name_matches, _strip_paren
from telemetry import get_telemetry


def _stops_by_direction_with_seq(
    data: Sequence[RouteStopEstimate],
) -> dict[Direction, list[tuple[int, str]]]:
    by_direction: dict[Direction, list[tuple[int, str]]] = {}
    for stop in data:
        if stop.sequence is None:
            continue
        name = _strip_paren(stop.stop_name)
        by_direction.setdefault(stop.direction, []).append((stop.sequence, name))
    return by_direction


def _downstream_names(stops: list[tuple[int, str]], kiosk_stop: str) -> list[str] | None:
    ordered = sorted(stops)
    kiosk_seq = next((seq for seq, name in ordered if _name_matches(kiosk_stop, name)), None)
    if kiosk_seq is None:
        return None
    return [name for seq, name in ordered if seq >= kiosk_seq]


def _iter_downstream_directions(
    data: Sequence[RouteStopEstimate],
    kiosk_stop: str,
    go_back: int | None = None,
) -> Iterator[tuple[Direction, list[str]]]:
    requested = Direction(go_back) if go_back is not None else None
    for direction, stops in _stops_by_direction_with_seq(data).items():
        if requested is not None and direction != requested:
            continue
        downstream = _downstream_names(stops, kiosk_stop)
        if downstream is not None:
            yield direction, downstream


def _is_traffic_controlled(stop: StopArrival | RouteStopEstimate) -> bool:
    return stop.status == StopStatus.NOT_STOPPING


def _dedup_stop_rows_by_direction(rows: Sequence[RouteStopEstimate]) -> list[RouteStopEstimate]:
    best: dict[Direction, RouteStopEstimate] = {}
    for row in rows:
        seq = row.sequence or 9999
        existing = best.get(row.direction)
        if existing is None or (existing.sequence or 9999) > seq:
            best[row.direction] = row
    return list(best.values())


def _rows_for_stop(
    rows: Sequence[RouteStopEstimate],
    stop_name: str,
    direction: int | None,
) -> list[RouteStopEstimate]:
    requested = Direction(direction) if direction is not None else None
    matched = [row for row in rows if stop_name in row.stop_name and (requested is None or row.direction == requested) and not _is_traffic_controlled(row)]
    return _dedup_stop_rows_by_direction(matched)


def _direction_label_from_info(
    route_info: dict[str, RouteInfo],
    route: str,
    direction: int | Direction,
) -> str:
    info = route_info.get(route) or RouteInfo(route)
    target = Direction(direction)
    dest = info.outbound_destination if target == Direction.OUTBOUND else info.inbound_destination
    if dest:
        return f"往{dest}"
    return "去程" if target == Direction.OUTBOUND else "回程"


def iter_scoped_stop_etas(
    eta_data: Sequence[StopArrival],
    route_info: dict[str, RouteInfo],
    stop_name: str,
    go_back: int | None,
) -> Iterator[tuple[StopArrival, str, str, Direction]]:
    for stop in eta_data:
        if stop.status == StopStatus.NOT_STOPPING:
            continue
        route = stop.route_name
        if not route or route not in route_info:
            continue
        if go_back is not None and stop.direction != Direction(go_back):
            continue
        if go_back is None and _is_terminal_direction(stop_name, route_info, route, stop.direction):
            get_telemetry().record_departure_decision(decision="filtered_terminal_direction")
            continue
        yield stop, route, route, stop.direction


def _is_terminal_direction(
    stop_name: str,
    route_info: dict[str, RouteInfo],
    route: str,
    direction: int | Direction,
) -> bool:
    info = route_info.get(route) or RouteInfo(route)
    outbound = info.outbound_destination
    inbound = info.inbound_destination
    is_circular = outbound and inbound and _name_matches(stop_name, outbound) and _name_matches(stop_name, inbound)
    if is_circular:
        return False
    terminus = outbound if Direction(direction) == Direction.OUTBOUND else inbound
    return bool(terminus) and _name_matches(stop_name, terminus)
