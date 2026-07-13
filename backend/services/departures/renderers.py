from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import datetime, timedelta

from providers.bus import BusProvider
from services.departures.classification import DepartureSection, _classify_stop
from services.departures.normalize import (
    TAIPEI_TZ,
    _dedup_stop_rows_by_direction,
    _direction_label_from_info,
    _downstream_names,
    _fuzzy_candidates,
    _lookup_route,
    _name_matches,
    _resolve_forward_match,
    _route_candidates,
    _stops_by_direction_with_seq,
)
from services.departures.provider import get_provider
from services.departures.snapshot import DepartureSnapshotUnavailable, build_departure_snapshot

_QUERY_FAILED = "查詢失敗，請稍後再試。"


async def _safe_provider_call[T](coro: Awaitable[T]) -> T | None:
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


@dataclass
class _RouteMiss:
    """Route not found at this stop, with ASR-rescue candidates (ranked, may be empty)."""

    route: str
    candidates: list[str]


def _rescue_prefix(best: str) -> str:
    """Confirmation cue for an auto-resolved ASR-rescue candidate.

    Deliberately never restates the mis-heard original: a compressive
    downstream model (Qwen3.5-4B) treats a repeated original term as license
    to echo it back in its final reply instead of the resolved canonical name
    `best` (eval E3 「虎尾科大」→虎尾科技大學 collapsed back to the mis-heard
    form; eval R9 "YO2"→Y02 likewise). `best` is the sentence's sole subject.
    """
    return f"最接近的是「{best}」。{best}的狀態："


def _is_real_status(text: str) -> bool:
    """True when a render result carries actual arrival status (not an error / miss).

    Bare "沒有" is deliberately *not* excluded here: `render_stop_on_route`'s
    legitimate negative answer ("沒有，201 不停X。") starts with it, and the
    two miss-message templates are already caught by the "本站沒有" prefix.
    """
    return text != _QUERY_FAILED and not text.startswith(("本站沒有", "查無", "路線 "))


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
) -> tuple[dict, list[dict]] | _RouteMiss | str:
    """Shared prologue for single-route renderers.

    Returns (route_info, estimate_data) on success, `_RouteMiss` when the route
    isn't served here (carrying similarity-ranked ASR-rescue candidates from the
    already-fetched `route_info` — no extra HTTP round-trip), or an error string
    on provider failure. `fuzzy` uses loose route-key matching (`_lookup_route`);
    otherwise exact dict lookup. Callers decide how to handle a miss:
    `render_arrivals` auto-resolves the top candidate (see there); the others
    stringify it via `_miss_to_str`.
    """
    provider = get_provider()
    route_info = await _safe_provider_call(provider.load_route_info(stop_name))
    if route_info is None:
        return _QUERY_FAILED

    info = _lookup_route(route_info, route) if fuzzy else route_info.get(route)
    route_id = info.get("id") if info is not None else None
    if not route_id:
        return _RouteMiss(route, _route_candidates(route, route_info))

    data = await _safe_provider_call(provider.fetch_route_estimate(route_id))
    if data is None:
        return _QUERY_FAILED
    return route_info, data


def _miss_to_str(miss: _RouteMiss) -> str:
    if miss.candidates:
        return f"本站沒有路線 {miss.route}。相近路線：{'、'.join(miss.candidates)}。"
    return f"本站沒有路線 {miss.route}。"


async def render_arrivals(
    route: str,
    stop_name: str,
    go_back: int | None = None,
    *,
    _rescue: bool = True,
) -> str:
    """Render `route` arrivals at `stop_name` as a kiosk-style string.

    ASR-rescue: on a mis-heard route (`_RouteMiss`) the renderer *itself*
    re-queries the top-ranked candidate and returns its real status behind a
    confirmation prefix. Qwen3.5-4B will not issue a second tool call on a text
    instruction — it fabricates an ETA instead — so resolving here is the only
    reliable way to keep the confirmation sentence truthful. `_rescue=False`
    guards the one-hop recursion.
    """
    resolved = await _resolve_route_estimate(route, stop_name, fuzzy=True)
    if isinstance(resolved, _RouteMiss):
        if _rescue and resolved.candidates:
            best = resolved.candidates[0]
            inner = await render_arrivals(best, stop_name, go_back, _rescue=False)
            if _is_real_status(inner):
                return f"{_rescue_prefix(best)}\n{inner}"
        return _miss_to_str(resolved)
    if isinstance(resolved, str):
        return resolved
    route_info, data = resolved

    matches = [stop for stop in data if stop_name in stop.get("stop_name", "") and (go_back is None or stop.get("direction") == go_back)]
    matches = _dedup_stop_rows_by_direction(matches)
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


async def render_route_stops(route: str, stop_name: str, *, _rescue: bool = True) -> str:
    """Render the full stop sequence (both directions) of `route`.

    ASR-rescue mirrors `render_arrivals`: on a mis-heard route the renderer
    re-queries the top-ranked candidate itself and returns its real stop list
    behind a confirmation prefix. `_rescue=False` guards the one-hop recursion.
    """
    resolved = await _resolve_route_estimate(route, stop_name)
    if isinstance(resolved, _RouteMiss):
        if _rescue and resolved.candidates:
            best = resolved.candidates[0]
            inner = await render_route_stops(best, stop_name, _rescue=False)
            if _is_real_status(inner):
                return f"{_rescue_prefix(best)}\n{inner}"
        return _miss_to_str(resolved)
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
    *,
    _rescue: bool = True,
) -> str:
    """Return a yes/no string: can you reach stop_name from kiosk on this route?

    Geo-aware: only directions where stop_name appears at or after the kiosk's
    position in the stop sequence count as 有. Substring matching so aliases
    like '斗六' match '斗六火車站'. LLM reads result verbatim.

    ASR-rescue mirrors `render_arrivals`: on a mis-heard route the renderer
    re-queries the top-ranked candidate itself and returns its real 有/沒有
    answer behind a confirmation prefix. `_rescue=False` guards the one-hop
    recursion.
    """
    resolved = await _resolve_route_estimate(route, kiosk_stop)
    if isinstance(resolved, _RouteMiss):
        if _rescue and resolved.candidates:
            best = resolved.candidates[0]
            inner = await render_stop_on_route(best, stop_name, kiosk_stop, _rescue=False)
            if _is_real_status(inner):
                return f"{_rescue_prefix(best)}\n{inner}"
        return _miss_to_str(resolved)
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
    destination: str,
    now: datetime,
) -> str:
    """Build ', 預計 HH:MM 抵達X（車程約N分鐘）' suffix, or '' when unknowable.

    Emitted only when the kiosk row itself has a live estimate AND the
    destination's estimate is later: TDX stop ETAs are per-stop "next bus",
    so when the kiosk bus hasn't departed the destination row belongs to an
    *earlier* trip already past the kiosk — quoting it produces impossible
    replies like「預計22:46發車，22:37抵達」. [eval v6 case D9]

    `destination` here must already be the resolved canonical stop name
    (caller's job) — quoting the raw user query would print an abbreviated
    or mis-heard form back at the rider instead of the real stop name.
    """
    if dest_rows:
        dest_est = dest_rows[0].get("estimate_seconds")
        kiosk_est = kiosk_row.get("estimate_seconds")
        if dest_est is not None and kiosk_est is not None and dest_est > kiosk_est:
            dest_arrival = now + timedelta(seconds=dest_est)
            travel_min = round((dest_est - kiosk_est) / 60)
            return f"，預計 {dest_arrival.strftime('%H:%M')} 抵達{destination}，車程約 {travel_min} 分鐘"

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
) -> tuple[list[tuple[str, int, DepartureSection]], set[str], str | None]:
    """Fetch estimate for one route; return (hits, all_downstream_stop_names, canonical_dest).

    hits: list of (display_text, sort_minutes, section).
    all_downstream: union of all downstream stop names seen (used for fuzzy remap).
    canonical_dest: the real stop name `destination` resolved to here (see
    `_resolve_forward_match`), or None if this route doesn't serve it — callers
    use it instead of the raw, possibly abbreviated/mis-heard `destination`
    string when building rider-facing text. [eval E3/E4/E8]
    """
    try:
        data = await provider.fetch_route_estimate(route_id)
    except Exception:
        return [], set(), None

    hits: list[tuple[str, int, DepartureSection]] = []
    all_downstream: set[str] = set()
    canonical_dest: str | None = None
    for direction, stops in _stops_by_direction_with_seq(data).items():
        if go_back is not None and direction != go_back:
            continue
        downstream = _downstream_names(stops, kiosk_stop)
        if downstream is None:
            continue
        all_downstream.update(downstream)
        canonical = _resolve_forward_match(destination, downstream)
        if canonical is None:
            continue
        canonical_dest = canonical

        dir_label = _direction_label_from_info(route_info, route_name, direction)
        if _name_matches(kiosk_stop, dir_label.removeprefix("往")):
            dir_label = "（循環）"

        kiosk_rows = [row for row in data if kiosk_stop in row.get("stop_name", "") and row.get("direction", 0) == direction]
        kiosk_rows = _dedup_stop_rows_by_direction(kiosk_rows)
        if kiosk_rows:
            c = _classify_stop(kiosk_rows[0], now)
            status_text = _with_schedule(_mark_incoming(c.status_text), c.scheduled_time)

            # Filter to the destination occurrence downstream of the kiosk boarding
            # point — circular routes repeat stop names, and picking an upstream
            # occurrence would report a shorter/negative travel time.
            kiosk_seq = kiosk_rows[0].get("stop_sequence") or 0
            dest_rows = [
                row
                for row in data
                if _name_matches(canonical, row.get("stop_name", "")) and row.get("direction") == direction and (row.get("stop_sequence") or 0) >= kiosk_seq
            ]
            dest_rows = _dedup_stop_rows_by_direction(dest_rows)
            dest_suffix = _dest_arrival_text(dest_rows, kiosk_rows[0], canonical, now)
            hits.append((f"{route_name} {dir_label}：{status_text}{dest_suffix}", c.sort_minutes, c.section))
        else:
            hits.append((f"{route_name} {dir_label}：無即時資料", 9999, DepartureSection.UNKNOWN))

    return hits, all_downstream, canonical_dest


async def render_arrivals_to_destination(
    destination: str,
    kiosk_stop: str,
    go_back: int | None = None,
    *,
    _rescue: bool = True,
) -> str:
    """Find routes to destination and return each route's next ETA at kiosk_stop.

    Single HTTP round-trip per route (stop sequence + ETA from the same
    fetch_route_estimate call). Results are sorted by arrival time so the LLM
    can directly answer "which is faster" without a follow-up tool call.
    Routes with no real-time data appear last with status_text from _classify_stop.

    ASR-rescue mirrors `render_arrivals`: on a mis-heard destination the renderer
    re-queries the top phonetic candidate itself and returns its real ETAs behind
    a confirmation prefix, rather than handing the 4B a candidate list it answers
    with a fabricated time. `_rescue=False` guards the one-hop recursion.
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

    async def _guarded(name: str, rid: str) -> tuple[list[tuple[str, int, DepartureSection]], set[str], str | None]:
        async with sem:
            return await _check_route_arrivals(name, rid, provider, kiosk_stop, go_back, destination, route_info, now)

    tasks_guarded = [_guarded(name, rid) for name, rid in valid if rid]
    results = await asyncio.gather(*tasks_guarded)
    raw = [item for hits, _, _ in results for item in hits]
    all_stops = {name for _, stops, _ in results for name in stops}
    # Real stop name `destination` resolved to (see `_resolve_forward_match`) —
    # used in rider-facing text below instead of a possibly abbreviated or
    # mis-heard `destination` string. Different routes can resolve to
    # differently-specific real names sharing the same prefix (a route ending
    # at "北港朝天宮" vs one continuing through bare "北港") — shortest wins,
    # same exact-match-preferred tie-break as `_resolve_forward_match` itself.
    # Falls back to `destination` when `raw` is empty (nothing resolved).
    canonical_candidates = {c for _, _, c in results if c}
    canonical = min(canonical_candidates, key=len) if canonical_candidates else destination

    if not raw:
        candidates = [name for name, _ in _fuzzy_candidates(destination, all_stops)]
        if _rescue and candidates:
            best = candidates[0]
            inner = await render_arrivals_to_destination(best, kiosk_stop, go_back, _rescue=False)
            if _is_real_status(inner):
                return f"{_rescue_prefix(best)}\n{inner}"
        if candidates:
            return f"本站沒有直達「{destination}」的路線。相近站名：{'、'.join(candidates[:5])}。"
        return f"本站沒有直達「{destination}」的路線。"

    raw.sort(key=lambda x: x[1])
    # Return only the highest-priority group so LLM sees one consistent situation.
    for section in (DepartureSection.AVAILABLE, DepartureSection.NOT_DEPARTED):
        group = [d for d, _, s in raw if s == section]
        if group:
            return "\n".join(group)
    # Every serving route has run its last bus today. Collapse the per-route
    # "末班駛離" lines into one closed conclusion the LLM can only copy: the 4B
    # was re-reading the granular list as 無直達 (route doesn't exist),
    # conflating "no bus left today" with "no such route". The wording avoids
    # 沒有 so it can't slip back into the 無直達 template. [eval v5 hole #1]
    if any(s == DepartureSection.LAST_DEPARTED for _, _, s in raw):
        return f"去{canonical}的公車今天班次都跑完了，末班已經開走囉。"
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
