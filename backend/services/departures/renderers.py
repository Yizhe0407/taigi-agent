from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from providers.bus import BusProvider
from services.departures.classification import DepartureSection, StopClassification, _classify_stop
from services.departures.fuzzy_match import (
    _fuzzy_candidates,
    _lookup_route,
    _resolve_forward_match,
    _route_candidates,
)
from services.departures.normalize import TAIPEI_TZ, _name_matches
from services.departures.provider import get_provider
from services.departures.rows import (
    _dedup_stop_rows_by_direction,
    _direction_label_from_info,
    _iter_downstream_directions,
    _rows_for_stop,
    _stops_by_direction_with_seq,
)
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

    Deliberately never restates the mis-heard original: the downstream 4B
    treats a repeated original term as licence to echo it back instead of the
    resolved `best` (eval E3 「虎尾科大」, R9 "YO2"), so `best` is the
    sentence's sole subject.
    """
    return f"最接近的是「{best}」。{best}的狀態："


def _is_real_status(text: str) -> bool:
    """True when a render result carries actual arrival status (not an error / miss).

    Bare "沒有" is deliberately *not* excluded here: `render_stop_on_route`'s
    legitimate negative answer ("沒有，201 不停X。") starts with it, and the
    two miss-message templates are already caught by the "本站沒有" prefix.
    """
    return text != _QUERY_FAILED and not text.startswith(("本站沒有", "查無", "路線 "))


def _with_schedule(status_text: str, scheduled_time: str | None) -> str:
    """Append next scheduled arrival time when bus hasn't departed yet."""
    if scheduled_time:
        return f"{status_text}（預計 {scheduled_time}）"
    return status_text


def _incoming_status_text(c: StopClassification) -> str:
    """Status of a bus heading *to the kiosk*, as the rider should hear it.

    A relative-time ETA (ends with 後) is anchored to a place — 到這站 — so it
    contrasts with 抵達{destination} instead of both ending in 站; that
    distinction matters for elderly riders and the line is read verbatim.
    """
    anchored = f"{c.status_text}到這站" if c.status_text.endswith("後") else c.status_text
    return _with_schedule(anchored, c.scheduled_time)


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


async def _rescue_or(
    candidates: list[str],
    rescue: bool,
    retry: Callable[[str], Awaitable[str]],
    miss: Callable[[], str],
) -> str:
    """Shared ASR-rescue core, factored out of 4 near-identical call sites.

    On the top phonetic `candidates[0]`, re-queries via `retry` and returns its
    real result behind `_rescue_prefix` — Qwen3.5-4B will not issue a second
    tool call on a text instruction, so resolving here (instead of handing the
    LLM a candidate list) is the only reliable way to keep the confirmation
    sentence truthful. Falls back to `miss()` when `rescue` is False,
    `candidates` is empty, or the retry itself came back empty
    (`_is_real_status`) — `retry` is expected to pass `_rescue=False` to guard
    the one-hop recursion.
    """
    if rescue and candidates:
        best = candidates[0]
        inner = await retry(best)
        if _is_real_status(inner):
            return f"{_rescue_prefix(best)}\n{inner}"
    return miss()


async def _render_with_rescue(
    route: str,
    stop_name: str,
    *,
    fuzzy: bool,
    rescue: bool,
    retry: Callable[[str], Awaitable[str]],
    body: Callable[[dict, list[dict]], str],
) -> str:
    """Shared prologue for single-route renderers.

    Resolves `route` at `stop_name` via `_resolve_route_estimate`; a mis-heard
    route (`_RouteMiss`) goes through `_rescue_or`, a provider failure returns
    its error string as-is, and a successful resolution hands
    `(route_info, estimate_data)` to `body` for the renderer-specific part.
    """
    resolved = await _resolve_route_estimate(route, stop_name, fuzzy=fuzzy)
    if isinstance(resolved, _RouteMiss):
        return await _rescue_or(resolved.candidates, rescue, retry, lambda: _miss_to_str(resolved))
    if isinstance(resolved, str):
        return resolved
    route_info, data = resolved
    return body(route_info, data)


async def render_arrivals(
    route: str,
    stop_name: str,
    go_back: int | None = None,
    *,
    _rescue: bool = True,
) -> str:
    """Render `route` arrivals at `stop_name` as a kiosk-style string.

    ASR-rescue: on a mis-heard route the renderer *itself* re-queries the
    top-ranked candidate and returns its real status behind a confirmation
    prefix (see `_rescue_or`). `_rescue=False` guards the one-hop recursion.
    """

    def _body(route_info: dict, data: list[dict]) -> str:
        matches = _rows_for_stop(data, stop_name, go_back)
        if not matches:
            return f"路線 {route} 不停 {stop_name}。"

        now = datetime.now(TAIPEI_TZ)
        results = []
        for stop in matches:
            status_text = _incoming_status_text(_classify_stop(stop, now))
            # Single-direction query: direction is already implied by kiosk config;
            # label only adds noise for TTS and short-response constraints.
            if go_back is None:
                label = _direction_label_from_info(route_info, route, stop.get("direction", 0))
                results.append(f"{label}：{status_text}")
            else:
                results.append(status_text)

        return "\n".join(results)

    return await _render_with_rescue(
        route,
        stop_name,
        fuzzy=True,
        rescue=_rescue,
        retry=lambda best: render_arrivals(best, stop_name, go_back, _rescue=False),
        body=_body,
    )


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

    if not any(sections.values()) and snapshot.summary.unknown_count == 0:
        return f"{stop_name} 目前無到站狀態資料"

    results = [f"{stop_name} 目前到站狀態："]
    for title, lines in sections.items():
        if lines:
            results.append(f"{title}：")
            results.extend(lines)
    # UNKNOWN routes (今日未營運 / 資料異常) are excluded from the section groups
    # above so they don't clutter a per-route list — but silently dropping them
    # would let the rider believe every route was checked. Surface the count
    # instead, consistent with `snapshot.summary.unknown_count`.
    if snapshot.summary.unknown_count:
        results.append(f"{snapshot.summary.unknown_count} 條路線今日未營運或無即時資料")

    return "\n".join(results)


async def render_route_stops(route: str, stop_name: str, *, _rescue: bool = True) -> str:
    """Render the full stop sequence (both directions) of `route`.

    ASR-rescue mirrors `render_arrivals`: on a mis-heard route the renderer
    re-queries the top-ranked candidate itself and returns its real stop list
    behind a confirmation prefix (see `_rescue_or`). `_rescue=False` guards the
    one-hop recursion.
    """

    def _body(route_info: dict, data: list[dict]) -> str:
        by_direction = _stops_by_direction_with_seq(data)
        if not by_direction:
            return f"查無路線 {route} 的站牌。"

        results = []
        for direction, stops in sorted(by_direction.items()):
            label = _direction_label_from_info(route_info, route, direction)
            ordered = [name for _, name in sorted(stops)]
            results.append(f"{label}：{'、'.join(ordered)}")

        return "\n".join(results)

    return await _render_with_rescue(
        route,
        stop_name,
        fuzzy=False,
        rescue=_rescue,
        retry=lambda best: render_route_stops(best, stop_name, _rescue=False),
        body=_body,
    )


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
    answer behind a confirmation prefix (see `_rescue_or`). `_rescue=False`
    guards the one-hop recursion.
    """

    def _body(route_info: dict, data: list[dict]) -> str:
        matched = [
            _direction_label_from_info(route_info, route, direction)
            for direction, downstream in sorted(_iter_downstream_directions(data, kiosk_stop), key=lambda item: item[0])
            if any(_name_matches(stop_name, n) for n in downstream)
        ]
        if matched:
            return f"有，{route} {'、'.join(matched)}有停{stop_name}。"
        return f"沒有，{route} 不停{stop_name}。"

    return await _render_with_rescue(
        route,
        kiosk_stop,
        fuzzy=False,
        rescue=_rescue,
        retry=lambda best: render_stop_on_route(best, stop_name, kiosk_stop, _rescue=False),
        body=_body,
    )


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


def _boarding_status(
    data: list[dict],
    kiosk_stop: str,
    direction: int,
    canonical_dest: str,
    now: datetime,
) -> tuple[str, int, DepartureSection]:
    """One direction's kiosk-side status text, with its sort key and section."""
    kiosk_rows = _rows_for_stop(data, kiosk_stop, direction)
    if not kiosk_rows:
        return "無即時資料", 9999, DepartureSection.UNKNOWN

    boarding = kiosk_rows[0]
    c = _classify_stop(boarding, now)

    # Keep only destination occurrences downstream of the boarding point —
    # circular routes repeat stop names, and an upstream occurrence would
    # report a shorter/negative travel time.
    boarding_seq = boarding.get("stop_sequence") or 0
    dest_rows = _dedup_stop_rows_by_direction(
        [
            row
            for row in data
            if _name_matches(canonical_dest, row.get("stop_name", "")) and row.get("direction") == direction and (row.get("stop_sequence") or 0) >= boarding_seq
        ]
    )
    dest_suffix = _dest_arrival_text(dest_rows, boarding, canonical_dest, now)
    return f"{_incoming_status_text(c)}{dest_suffix}", c.sort_minutes, c.section


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
    for direction, downstream in _iter_downstream_directions(data, kiosk_stop, go_back):
        all_downstream.update(downstream)
        canonical = _resolve_forward_match(destination, downstream)
        if canonical is None:
            continue
        canonical_dest = canonical

        dir_label = _direction_label_from_info(route_info, route_name, direction)
        # 「往<本站>」tells a rider standing here nothing — this direction departs
        # from the kiosk and comes back to it, so label it as the loop it is.
        if _name_matches(kiosk_stop, dir_label.removeprefix("往")):
            dir_label = "（循環）"

        status_text, sort_minutes, section = _boarding_status(data, kiosk_stop, direction, canonical, now)
        hits.append((f"{route_name} {dir_label}：{status_text}", sort_minutes, section))

    return hits, all_downstream, canonical_dest


def _pick_canonical_destination(
    results: list[tuple[list[tuple[str, int, DepartureSection]], set[str], str | None]],
    destination: str,
) -> str:
    """Resolve `destination` to the real stop name it matched, across all routes.

    Used in rider-facing text instead of the possibly abbreviated or mis-heard
    `destination`. Routes can resolve to differently-specific names sharing a
    prefix ("北港朝天宮" vs bare "北港"); shortest wins, the same
    exact-match-preferred tie-break as `_resolve_forward_match`. The string
    itself is the secondary key so tied lengths stay deterministic across
    restarts — `min()` over a `set` otherwise follows hash-seed order.
    """
    canonical_candidates = {c for _, _, c in results if c}
    return min(canonical_candidates, key=lambda s: (len(s), s)) if canonical_candidates else destination


def _summarize_route_hits(raw: list[tuple[str, int, DepartureSection]], canonical: str) -> str:
    """Sort route hits by ETA and return only the single most relevant status group.

    Only the highest-priority section (AVAILABLE, then NOT_DEPARTED) is
    returned so the LLM sees one consistent situation. When every route has run
    its last bus, the per-route "末班駛離" lines collapse into one closed
    conclusion: the 4B re-read the granular list as 無直達, conflating "no bus
    left today" with "no such route". The wording avoids 沒有 so it can't slip
    back into the 無直達 template. [eval v5 hole #1]
    """
    by_eta = sorted(raw, key=lambda hit: hit[1])
    for section in (DepartureSection.AVAILABLE, DepartureSection.NOT_DEPARTED):
        group = [d for d, _, s in by_eta if s == section]
        if group:
            return "\n".join(group)
    if any(s == DepartureSection.LAST_DEPARTED for _, _, s in by_eta):
        return f"去{canonical}的公車今天班次都跑完了，末班已經開走囉。"
    return "\n".join(d for d, _, __ in by_eta)


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
    a confirmation prefix (see `_rescue_or`), rather than handing the 4B a
    candidate list it answers with a fabricated time. `_rescue=False` guards the
    one-hop recursion.
    """
    provider = get_provider()
    route_info = await _safe_provider_call(provider.load_route_info(kiosk_stop))
    if not route_info:
        return _QUERY_FAILED

    now = datetime.now(TAIPEI_TZ)
    # Firing all N routes in parallel causes 429 storms when the cache is cold.
    sem = asyncio.Semaphore(3)

    async def _guarded(name: str, route_id: str) -> tuple[list[tuple[str, int, DepartureSection]], set[str], str | None]:
        async with sem:
            return await _check_route_arrivals(name, route_id, provider, kiosk_stop, go_back, destination, route_info, now)

    results = await asyncio.gather(*(_guarded(name, info["id"]) for name, info in route_info.items() if info.get("id")))
    raw = [item for hits, _, _ in results for item in hits]
    all_stops = {name for _, stops, _ in results for name in stops}
    canonical = _pick_canonical_destination(results, destination)

    if not raw:
        candidates = [name for name, _ in _fuzzy_candidates(destination, all_stops)]

        def _miss() -> str:
            if candidates:
                return f"本站沒有直達「{destination}」的路線。相近站名：{'、'.join(candidates[:5])}。"
            return f"本站沒有直達「{destination}」的路線。"

        return await _rescue_or(
            candidates,
            _rescue,
            lambda best: render_arrivals_to_destination(best, kiosk_stop, go_back, _rescue=False),
            _miss,
        )

    return _summarize_route_hits(raw, canonical)


async def render_routes_at_stop(stop_name: str) -> str:
    """Render the list of routes serving `stop_name` (no ETA, no classify)."""
    provider = get_provider()
    data = await _safe_provider_call(provider.fetch_routes_at_stop(stop_name))
    if data is None:
        return _QUERY_FAILED

    if not data:
        return f"查無 {stop_name} 站牌。"

    # One line per route: the same route appears once per direction upstream.
    routes = dict.fromkeys(r.get("sub_route_name", "?") for r in data)
    return f"{stop_name} 停靠路線：\n" + "\n".join(routes)
