from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from datetime import datetime, timedelta
from typing import TypeVar

from providers.bus import BusProvider
from services.departures.classification import DepartureSection, _classify_stop
from services.departures.normalize import (
    TAIPEI_TZ,
    _direction_label_from_info,
    _downstream_names,
    _fuzzy_candidates,
    _lookup_route,
    _name_matches,
    _stops_by_direction_with_seq,
)
from services.departures.provider import get_provider
from services.departures.snapshot import DepartureSnapshotUnavailable, build_departure_snapshot

_T = TypeVar("_T")
_QUERY_FAILED = "查詢失敗，請稍後再試。"


async def _safe_provider_call[T](coro: Awaitable[_T]) -> _T | None:
    """Run a provider coroutine; return None on any exception."""
    try:
        return await coro
    except Exception:
        return None


_SECTION_GROUP_LABEL: dict[DepartureSection, str] = {
    DepartureSection.AVAILABLE: "有車",
    DepartureSection.NOT_DEPARTED: "尚未發車",
    DepartureSection.LAST_DEPARTED: "末班已過",
}


def _mark_incoming(status_text: str) -> str:
    """Relative-time ETA (ends with 後) → append 到這站. Anchors the arrival to a
    *place* (這站 = here) so it contrasts with 抵達{destination} rather than both
    ending in 站; critical for elderly riders. Reads naturally if copied verbatim."""
    return f"{status_text}到這站" if status_text.endswith("後") else status_text


def _with_schedule(status_text: str, scheduled_time: str | None) -> str:
    """Append next scheduled arrival time when bus hasn't departed yet."""
    if scheduled_time:
        return f"{status_text}（預計 {scheduled_time}）"
    return status_text


async def _resolve_route_estimate(
    route: str,
    stop_name: str,
    *,
    fuzzy: bool = False,
    not_found_msg: str | None = None,
) -> tuple[dict, list[dict]] | str:
    """Shared prologue for single-route renderers.

    Returns (route_info, estimate_data) on success, or a user-facing error
    string. `fuzzy` uses loose route-key matching (`_lookup_route`); otherwise
    exact dict lookup. `not_found_msg` overrides the "route not at stop" text.
    """
    provider = get_provider()
    route_info = await _safe_provider_call(provider.load_route_info(stop_name))
    if route_info is None:
        return _QUERY_FAILED

    info = _lookup_route(route_info, route) if fuzzy else route_info.get(route)
    route_id = info.get("id") if info is not None else None
    if not route_id:
        return not_found_msg or f"本站沒有路線 {route}。"

    data = await _safe_provider_call(provider.fetch_route_estimate(route_id))
    if data is None:
        return _QUERY_FAILED
    return route_info, data


async def render_arrivals(
    route: str,
    stop_name: str,
    go_back: int | None = None,
) -> str:
    """Render `route` arrivals at `stop_name` as a kiosk-style string."""
    resolved = await _resolve_route_estimate(route, stop_name, fuzzy=True)
    if isinstance(resolved, str):
        return resolved
    route_info, data = resolved

    matches = [stop for stop in data if stop_name in stop.get("stop_name", "") and (go_back is None or stop.get("direction") == go_back)]
    if not matches:
        return f"路線 {route} 不停 {stop_name}。"

    now = datetime.now(TAIPEI_TZ)
    results = []
    for stop in matches:
        stop_direction = stop.get("direction", 0)
        c = _classify_stop(stop, now)
        status_text = _with_schedule(_mark_incoming(c.status_text), c.scheduled_time)
        # Single-direction query: direction is already implied by kiosk config;
        # label only adds noise for TTS and short-response constraints.
        if go_back is None:
            label = _direction_label_from_info(route_info, route, stop_direction)
            results.append(f"{label}：{status_text}")
        else:
            results.append(status_text)

    return "\n".join(results)


async def render_stop_arrival_statuses(
    stop_name: str,
    go_back: int | None = None,
) -> str:
    """Render every route currently serving `stop_name` grouped by section."""
    try:
        snapshot = await build_departure_snapshot(stop_name, go_back)
    except DepartureSnapshotUnavailable:
        return _QUERY_FAILED

    sections: dict[str, list[str]] = {label: [] for label in _SECTION_GROUP_LABEL.values()}
    for r in snapshot.routes:
        group = _SECTION_GROUP_LABEL.get(r.section)
        if group is None:
            continue
        sections[group].append(f"{r.route} {r.direction}：{_with_schedule(r.status_text, r.scheduled_time)}")

    if not any(sections.values()):
        return f"{stop_name} 目前無到站狀態資料"

    results = [f"{stop_name} 目前到站狀態："]
    for title, lines in sections.items():
        if lines:
            results.append(f"{title}：")
            results.extend(lines)

    return "\n".join(results)


async def render_route_stops(route: str, stop_name: str) -> str:
    """Render the full stop sequence (both directions) of `route`."""
    resolved = await _resolve_route_estimate(route, stop_name)
    if isinstance(resolved, str):
        return resolved
    route_info, data = resolved

    by_direction = _stops_by_direction_with_seq(data)
    if not by_direction:
        return f"查無路線 {route} 的站牌。"

    results = []
    for direction, stops in sorted(by_direction.items()):
        label = _direction_label_from_info(route_info, route, direction)
        ordered = [name for _, name in sorted(stops)]
        results.append(f"{label}：{'、'.join(ordered)}")

    return "\n".join(results)


async def render_stop_on_route(
    route: str,
    stop_name: str,
    kiosk_stop: str,
) -> str:
    """Return a yes/no string: can you reach stop_name from kiosk on this route?

    Geo-aware: only directions where stop_name appears at or after the kiosk's
    position in the stop sequence count as 有. Substring matching so aliases
    like '斗六' match '斗六火車站'. LLM reads result verbatim.
    """
    resolved = await _resolve_route_estimate(
        route,
        kiosk_stop,
        not_found_msg=f"在 {kiosk_stop} 找不到停靠路線 {route}",
    )
    if isinstance(resolved, str):
        return resolved
    route_info, data = resolved

    matched: list[str] = []
    for gb, stops in sorted(_stops_by_direction_with_seq(data).items()):
        downstream = _downstream_names(stops, kiosk_stop)
        if downstream is None:
            continue
        if any(_name_matches(stop_name, n) for n in downstream):
            matched.append(_direction_label_from_info(route_info, route, gb))

    if matched:
        return f"有，{route} {'、'.join(matched)}有停{stop_name}。"
    return f"沒有，{route} 不停{stop_name}。"


def _dest_arrival_text(
    dest_rows: list[dict],
    kiosk_row: dict,
    downstream: list[str],
    destination: str,
    now: datetime,
) -> str:
    """Build ', 預計 HH:MM 抵達X（車程約N分鐘）' suffix, or ', 共N站' fallback."""
    if dest_rows:
        dest_est = dest_rows[0].get("estimate_seconds")
        if dest_est is not None:
            dest_arrival = now + timedelta(seconds=dest_est)
            kiosk_est = kiosk_row.get("estimate_seconds")
            travel_str = ""
            if kiosk_est is not None and dest_est > kiosk_est:
                travel_min = round((dest_est - kiosk_est) / 60)
                travel_str = f"，車程約 {travel_min} 分鐘"
            return f"，預計 {dest_arrival.strftime('%H:%M')} 抵達{destination}{travel_str}"

    return ""


async def _check_route_arrivals(
    route_name: str,
    route_id: str,
    provider: BusProvider,
    kiosk_stop: str,
    go_back: int | None,
    destination: str,
    route_info: dict,
    now: datetime,
) -> tuple[list[tuple[str, int, DepartureSection]], set[str]]:
    """Fetch estimate for one route; return (hits, all_downstream_stop_names).

    hits: list of (display_text, sort_minutes, section).
    all_downstream: union of all downstream stop names seen (used for fuzzy remap).
    """
    try:
        data = await provider.fetch_route_estimate(route_id)
    except Exception:
        return [], set()

    hits: list[tuple[str, int, DepartureSection]] = []
    all_downstream: set[str] = set()
    for direction, stops in _stops_by_direction_with_seq(data).items():
        if go_back is not None and direction != go_back:
            continue
        downstream = _downstream_names(stops, kiosk_stop)
        if downstream is None:
            continue
        all_downstream.update(downstream)
        if not any(_name_matches(destination, n) for n in downstream):
            continue

        dir_label = _direction_label_from_info(route_info, route_name, direction)
        if _name_matches(kiosk_stop, dir_label.removeprefix("往")):
            dir_label = "（循環）"

        kiosk_rows = [row for row in data if kiosk_stop in row.get("stop_name", "") and row.get("direction", 0) == direction]
        if kiosk_rows:
            c = _classify_stop(kiosk_rows[0], now)
            status_text = _with_schedule(_mark_incoming(c.status_text), c.scheduled_time)

            dest_rows = [row for row in data if _name_matches(destination, row.get("stop_name", "")) and row.get("direction") == direction]
            dest_suffix = _dest_arrival_text(dest_rows, kiosk_rows[0], downstream, destination, now)
            hits.append((f"{route_name} {dir_label}：{status_text}{dest_suffix}", c.sort_minutes, c.section))
        else:
            hits.append((f"{route_name} {dir_label}：無即時資料", 9999, DepartureSection.UNKNOWN))

    return hits, all_downstream


async def _remap_destination_via_fuzzy(
    destination: str,
    all_stops: set[str],
    kiosk_stop: str,
    go_back: int | None,
) -> str | None:
    """Try fuzzy remap of destination; return result string or None if no candidate.

    score >= 0.8: silent auto-remap (e.g. '雲林高鐵站' → '高鐵雲林站')
    0.35 <= score < 0.8: return hint text for LLM to clarify
    """
    candidates = _fuzzy_candidates(destination, all_stops)
    if not candidates:
        return None
    best_name, best_score = candidates[0]
    if best_score >= 0.8:
        return await render_arrivals_to_destination(best_name, kiosk_stop, go_back, _allow_remap=False)
    top = "、".join(name for name, _ in candidates[:3])
    return f"查無「{destination}」站名。本站路線鄰近站名：{top}。"


async def render_arrivals_to_destination(
    destination: str,
    kiosk_stop: str,
    go_back: int | None = None,
    *,
    _allow_remap: bool = True,
) -> str:
    """Find routes to destination and return each route's next ETA at kiosk_stop.

    Single HTTP round-trip per route (stop sequence + ETA from the same
    fetch_route_estimate call). Results are sorted by arrival time so the LLM
    can directly answer "which is faster" without a follow-up tool call.
    Routes with no real-time data appear last with status_text from _classify_stop.
    """
    provider = get_provider()
    route_info = await _safe_provider_call(provider.load_route_info(kiosk_stop))
    if not route_info:
        return _QUERY_FAILED

    now = datetime.now(TAIPEI_TZ)
    valid = [(name, info.get("id")) for name, info in route_info.items()]
    # Semaphore limits concurrent TDX calls; firing all N routes in parallel
    # causes 429 storms when cache is cold.
    sem = asyncio.Semaphore(3)

    async def _guarded(name: str, rid: str) -> tuple[list[tuple[str, int]], set[str]]:
        async with sem:
            return await _check_route_arrivals(name, rid, provider, kiosk_stop, go_back, destination, route_info, now)

    tasks_guarded = [_guarded(name, rid) for name, rid in valid if rid]
    results = await asyncio.gather(*tasks_guarded)
    raw = [item for hits, _ in results for item in hits]
    all_stops = {name for _, stops in results for name in stops}

    if not raw:
        if _allow_remap:
            remapped = await _remap_destination_via_fuzzy(destination, all_stops, kiosk_stop, go_back)
            if remapped is not None:
                return remapped
        return f"本站沒有直達{destination}的路線"

    raw.sort(key=lambda x: x[1])
    # Return only the highest-priority group so LLM sees one consistent situation.
    for section in (DepartureSection.AVAILABLE, DepartureSection.NOT_DEPARTED, DepartureSection.LAST_DEPARTED):
        group = [d for d, _, s in raw if s == section]
        if group:
            return "\n".join(group)
    return "\n".join(d for d, _, __ in raw)


async def render_routes_at_stop(stop_name: str) -> str:
    """Render the list of routes serving `stop_name` (no ETA, no classify)."""
    provider = get_provider()
    data = await _safe_provider_call(provider.fetch_routes_at_stop(stop_name))
    if data is None:
        return _QUERY_FAILED

    if not data:
        return f"查無 {stop_name} 站牌。"

    seen: set[str] = set()
    lines: list[str] = []
    for r in data:
        name = r.get("sub_route_name", "?")
        if name not in seen:
            seen.add(name)
            lines.append(name)

    return f"{stop_name} 停靠路線：\n" + "\n".join(lines)
