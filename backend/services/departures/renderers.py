from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from datetime import datetime
from typing import TypeVar

from providers.bus import BusProvider
from services.departures.classification import DepartureSection, _classify_stop
from services.departures.normalize import (
    TAIPEI_TZ,
    _as_int,
    _direction_label_from_info,
    _downstream_names,
    _fuzzy_candidates,
    _is_terminal_direction,
    _lookup_route,
    _name_matches,
    _stops_by_direction_with_seq,
    _strip_paren,
)
from services.departures.provider import get_provider

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
    """Relative-time ETA (ends with 後) → append 來車 so the reader knows this is
    the bus arriving at the kiosk, not the travel time to the destination."""
    return f"{status_text}來車" if status_text.endswith("後") else status_text


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
    route_id = _as_int(info.get("id")) if info is not None else None
    if route_id is None:
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

    matches = [stop for stop in data if stop_name in stop.get("StopName", "") and (go_back is None or stop.get("GoBack") == go_back)]
    if not matches:
        return f"路線 {route} 不停 {stop_name}。"

    now = datetime.now(TAIPEI_TZ)
    results = []
    for stop in matches:
        stop_go_back = stop.get("GoBack", 1)
        status_text = _mark_incoming(_classify_stop(stop, now).status_text)
        # Single-direction query: direction is already implied by kiosk config;
        # label only adds noise for TTS and short-response constraints.
        if go_back is None:
            label = _direction_label_from_info(route_info, route, stop_go_back)
            results.append(f"{label}：{status_text}")
        else:
            results.append(status_text)

    return "\n".join(results)


async def render_stop_arrival_statuses(
    stop_name: str,
    go_back: int | None = None,
) -> str:
    """Render every route currently serving `stop_name` grouped by section."""
    provider = get_provider()
    try:
        eta_data, route_info = await asyncio.gather(
            provider.fetch_eta_at_stop(stop_name),
            provider.load_route_info(stop_name),
        )
    except Exception:
        return _QUERY_FAILED

    route_by_id = {info["id"]: name for name, info in route_info.items()}
    sections: dict[str, list[str]] = {
        "有車": [],
        "尚未發車": [],
        "末班已過": [],
    }
    seen: set[str] = set()
    now = datetime.now(TAIPEI_TZ)

    for stop in eta_data:
        stop_go_back = stop.get("GoBack", 1)

        try:
            route_id = int(stop["xno"])
        except (KeyError, TypeError, ValueError):
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
        # UNKNOWN rows are skipped — the agent's kiosk-style output only
        # surfaces the three actionable groups (matches the legacy renderer).
        group = _SECTION_GROUP_LABEL.get(c.section)
        if group is None:
            continue

        label = _direction_label_from_info(route_info, route, stop_go_back)
        line = f"{route} {label}：{c.status_text}"
        if line in seen:
            continue
        seen.add(line)
        sections[group].append(line)

    if not seen:
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

    by_direction: dict[int, list[tuple[int, str]]] = {}
    for stop in data:
        go_back = stop.get("GoBack", 1)
        seq = stop.get("SeqNo", 0)
        name = _strip_paren(stop.get("StopName", ""))
        by_direction.setdefault(go_back, []).append((seq, name))

    if not by_direction:
        return f"查無路線 {route} 的站牌。"

    results = []
    for go_back, stops in sorted(by_direction.items()):
        label = _direction_label_from_info(route_info, route, go_back)
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


async def _check_route_arrivals(
    route_name: str,
    route_id: int,
    provider: BusProvider,
    kiosk_stop: str,
    go_back: int | None,
    destination: str,
    route_info: dict,
    now: datetime,
) -> tuple[list[tuple[str, int]], set[str]]:
    """Fetch estimate for one route; return (hits, all_downstream_stop_names).

    hits: list of (display_text, sort_minutes) for directions that serve destination.
    all_downstream: union of all downstream stop names seen (used for fuzzy remap).
    """
    try:
        data = await provider.fetch_route_estimate(route_id)
    except Exception:
        return [], set()

    hits: list[tuple[str, int]] = []
    all_downstream: set[str] = set()
    for gb, stops in _stops_by_direction_with_seq(data).items():
        if go_back is not None and gb != go_back:
            continue
        downstream = _downstream_names(stops, kiosk_stop)
        if downstream is None:
            continue
        all_downstream.update(downstream)
        if not any(_name_matches(destination, n) for n in downstream):
            continue

        direction = _direction_label_from_info(route_info, route_name, gb)
        if _name_matches(kiosk_stop, direction.removeprefix("往")):
            direction = "（循環）"

        kiosk_rows = [row for row in data if kiosk_stop in row.get("StopName", "") and row.get("GoBack", 1) == gb]
        if kiosk_rows:
            c = _classify_stop(kiosk_rows[0], now)
            status_text = _mark_incoming(c.status_text)
            hits.append((f"{route_name} {direction}：{status_text}", c.sort_minutes))
        else:
            hits.append((f"{route_name} {direction}：無即時資料", 9999))

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
    valid = [(name, _as_int(info.get("id"))) for name, info in route_info.items()]
    tasks = [_check_route_arrivals(name, rid, provider, kiosk_stop, go_back, destination, route_info, now) for name, rid in valid if rid is not None]
    results = await asyncio.gather(*tasks)
    raw = [item for hits, _ in results for item in hits]
    all_stops = {name for _, stops in results for name in stops}

    if not raw:
        if _allow_remap:
            remapped = await _remap_destination_via_fuzzy(destination, all_stops, kiosk_stop, go_back)
            if remapped is not None:
                return remapped
        return f"本站沒有直達{destination}的路線"

    raw.sort(key=lambda x: x[1])
    real_time = [(d, m) for d, m in raw if m < 9999]
    if real_time:
        return "\n".join(d for d, _ in real_time)
    return "\n".join(d for d, _ in raw)


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
        name = r.get("name", "?")
        if name not in seen:
            seen.add(name)
            lines.append(name)

    return f"{stop_name} 停靠路線：\n" + "\n".join(lines)
