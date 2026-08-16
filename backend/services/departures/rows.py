"""TDX row shaping: turn raw provider rows into the kiosk's view of them.

Everything here operates on the flat TDX-native dicts the `BusProvider`
Protocol yields (`sub_route_name`, `direction` 0=去程/1=回程, `stop_status`,
`stop_sequence`, `estimate_seconds`). The shaping rules are shared by the
renderers and the snapshot builders so both see the same rows:

- scope filtering — StopStatus 2 (交管不停) rows are dropped before anything can
  classify them, and directions where this stop is the terminus are dropped when
  the kiosk is configured for 去回程都有 (`iter_scoped_stop_etas`).
- dedup — circular routes list the kiosk twice per direction; the boarding
  (min stop_sequence) occurrence wins (`_dedup_stop_rows_by_direction`).
- downstream derivation — stops at or after the kiosk, per direction
  (`_iter_downstream_directions`).

Classification of the shaped rows lives in `classification.py`, not here.
"""

from __future__ import annotations

from collections.abc import Iterator

from services.departures.normalize import _name_matches, _strip_paren
from telemetry import get_telemetry


def _stops_by_direction_with_seq(
    data: list[dict],
) -> dict[int, list[tuple[int, str]]]:
    """Group route_estimate rows by direction, retaining (seq, stripped_name).

    TDX fields: direction (0/1), stop_sequence (int), stop_name (str).
    """
    by_direction: dict[int, list[tuple[int, str]]] = {}
    for stop in data:
        direction = stop.get("direction", 0)
        seq = stop.get("stop_sequence")
        if seq is None:
            continue
        name = _strip_paren(stop.get("stop_name", ""))
        by_direction.setdefault(direction, []).append((seq, name))
    return by_direction


def _downstream_names(
    stops: list[tuple[int, str]],
    kiosk_stop: str,
) -> list[str] | None:
    """Return stop names at or after the first kiosk-matching position.

    None when the kiosk doesn't appear in this direction — caller skips it.
    Includes the kiosk itself so「有沒有停 X」when X is the kiosk answers 有.

    `stops` arrives in raw TDX row order, so it is sorted first: on circular
    routes the kiosk name repeats as the loop-completion arrival, and taking
    that later occurrence as the boarding point would report a shorter
    downstream list than is actually reachable.
    """
    ordered = sorted(stops)
    kiosk_seq = next(
        (s for s, n in ordered if _name_matches(kiosk_stop, n)),
        None,
    )
    if kiosk_seq is None:
        return None
    return [n for s, n in ordered if s >= kiosk_seq]


def _iter_downstream_directions(
    data: list[dict],
    kiosk_stop: str,
    go_back: int | None = None,
) -> Iterator[tuple[int, list[str]]]:
    """Yield (direction, stop names from the kiosk onward) per served direction.

    Directions where the kiosk never appears are skipped. `go_back` restricts
    to one TDX Direction (0=去程, 1=回程); None means both.
    """
    for direction, stops in _stops_by_direction_with_seq(data).items():
        if go_back is not None and direction != go_back:
            continue
        downstream = _downstream_names(stops, kiosk_stop)
        if downstream is not None:
            yield direction, downstream


def _is_traffic_controlled(stop: dict) -> bool:
    """True for TDX StopStatus 2 (交管不停靠) — a row callers must drop before
    `_classify_stop`, never classify. Shared by every call site that walks raw
    TDX rows (`iter_scoped_stop_etas`, `render_arrivals`, `_check_route_arrivals`,
    `build_route_detail`) so a traffic-controlled stop is silently skipped
    everywhere instead of surfacing as "狀態不明" wherever a caller forgets.
    """
    return stop.get("stop_status") == 2


def _dedup_stop_rows_by_direction(rows: list[dict]) -> list[dict]:
    """Collapse duplicate stop occurrences to one row per direction.

    Circular routes yield two `fetch_route_estimate` rows for the kiosk stop
    name within the same direction — the boarding point (min stop_sequence)
    and the loop-completion arrival (max stop_sequence). Keeping the min-seq
    row matches `TdxBusProvider._build_route_info`'s boarding-UID selection,
    so callers see one consistent ETA instead of two conflicting ones.
    """
    best: dict[int, dict] = {}
    for row in rows:
        direction = row.get("direction", 0)
        seq = row.get("stop_sequence") or 9999
        existing = best.get(direction)
        if existing is None or (existing.get("stop_sequence") or 9999) > seq:
            best[direction] = row
    return list(best.values())


def _rows_for_stop(rows: list[dict], stop_name: str, direction: int | None) -> list[dict]:
    """Boarding rows for `stop_name`, at most one per direction.

    Substring name match (aliases like '斗六' hit '斗六火車站'); `direction`
    restricts to one TDX Direction (0/1) or None for both; StopStatus 2
    (交管不停) rows are dropped before they can reach `_classify_stop`, and
    circular-route repeats collapse to the boarding occurrence.
    """
    matched = [
        row
        for row in rows
        if stop_name in row.get("stop_name", "") and (direction is None or row.get("direction", 0) == direction) and not _is_traffic_controlled(row)
    ]
    return _dedup_stop_rows_by_direction(matched)


def _direction_label_from_info(
    route_info: dict[str, dict],
    route: str,
    direction: int,
) -> str:
    """Return '往<dest>' label for the given TDX Direction (0=去程, 1=回程)."""
    info = route_info.get(route, {})
    if direction == 0:
        dest = info.get("go_dest", "")
        return f"往{dest}" if dest else "去程"
    dest = info.get("back_dest", "")
    return f"往{dest}" if dest else "回程"


def iter_scoped_stop_etas(
    eta_data: list[dict],
    route_info: dict[str, dict],
    stop_name: str,
    go_back: int | None,
) -> Iterator[tuple[dict, str, str, int]]:
    """Yield (stop_row, route, route_id, direction) for ETA rows in kiosk scope.

    TDX fields: sub_route_name, direction (0/1), stop_status.
    StopStatus 2 (交管不停靠) is silently skipped.
    go_back parameter uses TDX Direction encoding: 0=去程, 1=回程.
    """
    for stop in eta_data:
        if _is_traffic_controlled(stop):
            continue
        stop_direction = stop.get("direction", 0)
        sub_route_name = stop.get("sub_route_name")
        if not sub_route_name or sub_route_name not in route_info:
            continue

        if go_back is not None:
            if stop_direction != go_back:
                continue
        elif _is_terminal_direction(stop_name, route_info, sub_route_name, stop_direction):
            get_telemetry().record_departure_decision(decision="filtered_terminal_direction")
            continue

        yield stop, sub_route_name, sub_route_name, stop_direction


def _is_terminal_direction(
    stop_name: str,
    route_info: dict[str, dict],
    route: str,
    direction: int,
) -> bool:
    """True when this stop is the non-circular terminus for this direction.

    Circular routes (go_dest == back_dest == stop) always return False so
    they are shown — the bus departs from here even though it also returns here.
    """
    info = route_info.get(route, {})
    go_dest = info.get("go_dest", "")
    back_dest = info.get("back_dest", "")
    # Require both termini non-empty: empty string is a substring of everything,
    # so "" would cause a false-positive circular match.
    is_circular = go_dest and back_dest and _name_matches(stop_name, go_dest) and _name_matches(stop_name, back_dest)
    if is_circular:
        return False
    terminus = go_dest if direction == 0 else back_dest
    return bool(terminus) and _name_matches(stop_name, terminus)
