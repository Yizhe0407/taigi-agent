"""Departure decisions for the kiosk stop — single classification source.

This module owns the rules that turn a raw bus ETA row into a user-facing
decision. Both the HTTP API (structured dataclasses) and the LLM agent
(string renderers) call into the same `_classify_stop` so their wording
cannot drift.

Provider I/O is reached through the `BusProvider` Protocol; the active
instance lives at module scope (`_provider`) and can be swapped via
`set_provider()` — tests inject a fake, production wires the concrete
`YunlinEbusProvider`.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from zoneinfo import ZoneInfo

from providers.bus import BusProvider
from providers.yunlin_ebus import YunlinEbusProvider

TAIPEI_TZ = ZoneInfo("Asia/Taipei")

_PAREN_RE = re.compile(r"\s*[（(][^）)]{0,40}[）)]\s*")
_ONES_ZH = ["", "一", "二", "三", "四", "五", "六", "七", "八", "九"]


def _strip_paren(name: str) -> str:
    """Remove parenthetical suffixes from stop names: 持法媽祖宮(頂溪) → 持法媽祖宮."""
    return _PAREN_RE.sub("", name).strip()


def _mins_zh(n: int) -> str:
    """Integer minutes 0–99 → natural Chinese count."""
    if n <= 0:
        return "零"
    if n < 10:
        return _ONES_ZH[n]
    if n < 20:
        return "十" + _ONES_ZH[n % 10]
    return _ONES_ZH[n // 10] + "十" + _ONES_ZH[n % 10]


_log = logging.getLogger(__name__)
_provider: BusProvider = YunlinEbusProvider()


def get_provider() -> BusProvider:
    return _provider


def set_provider(provider: BusProvider) -> None:
    """Swap the active `BusProvider` (boot-time wiring, multi-region rollouts).

    Prefer `provider_override()` from test / scoped code so the previous
    instance is restored automatically.
    """
    global _provider
    _provider = provider


@contextmanager
def provider_override(provider: BusProvider) -> Iterator[BusProvider]:
    """Scope a temporary BusProvider; restore the previous one on exit.

    Use this from tests and short-lived swaps so a thrown exception or an
    early return cannot leave the module pinned to a fake provider.
    """
    previous = _provider
    set_provider(provider)
    try:
        yield provider
    finally:
        set_provider(previous)


class DepartureSection(StrEnum):
    AVAILABLE = "available"
    NOT_DEPARTED = "not_departed"
    LAST_DEPARTED = "last_departed"
    UNKNOWN = "unknown"


class DepartureDecision(StrEnum):
    ARRIVING_SOON = "arriving_soon"
    CAN_WAIT = "can_wait"
    LONG_WAIT = "long_wait"
    SCHEDULED = "scheduled"
    NOT_DEPARTED = "not_departed"
    LAST_DEPARTED = "last_departed"
    UNKNOWN = "unknown"


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


# ── classification ────────────────────────────────────────────────────────────


def _scheduled_minutes_from_now(scheduled_time: str, now: datetime) -> int:
    """Convert a HH:MM scheduled time to minutes from *now*, handling midnight wrap.

    If the scheduled time has already passed today, treat it as tomorrow
    (add 1440).  Returns 9999 on parse failure so malformed rows sort last.
    """
    try:
        h, m = map(int, scheduled_time.split(":"))
    except (ValueError, AttributeError):
        return 9999
    scheduled_total = h * 60 + m
    now_total = now.hour * 60 + now.minute
    delta = scheduled_total - now_total
    if delta < 0:
        delta += 24 * 60
    return delta


def _as_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _classify_stop(
    stop: dict,
    now: datetime,
) -> tuple[
    DepartureSection,
    DepartureDecision,
    str,
    str,
    int | None,
    str | None,
    int,
    int,
]:
    """Classify a bus ETA row into a user-facing departure decision.

    Returns (section, decision, status_text, decision_text, minutes,
             scheduled_time, sort_priority, sort_minutes).

    ``minutes`` is the real-time ETA shown to the user (None when unavailable).
    ``sort_minutes`` is always an int (minutes-from-now) used only for sorting;
    scheduled-only rows derive this from ComeTime so the list stays time-ordered
    across midnight boundaries.
    """
    raw_value = stop.get("Value")
    value = _as_int(raw_value)
    scheduled_time = str(stop.get("ComeTime") or "").strip() or None

    if raw_value is not None and value is None:
        return (
            DepartureSection.UNKNOWN,
            DepartureDecision.UNKNOWN,
            "狀態不明",
            "資料異常",
            None,
            scheduled_time,
            400,
            9999,
        )

    if value == -3:
        return (
            DepartureSection.LAST_DEPARTED,
            DepartureDecision.LAST_DEPARTED,
            "末班駛離",
            "末班已過",
            None,
            scheduled_time,
            300,
            9999,
        )

    if value is not None:
        if value < 0:
            return (
                DepartureSection.UNKNOWN,
                DepartureDecision.UNKNOWN,
                "狀態不明",
                "資料異常",
                None,
                scheduled_time,
                400,
                9999,
            )
        if value <= 3:
            return (
                DepartureSection.AVAILABLE,
                DepartureDecision.ARRIVING_SOON,
                "即將到站",
                "即將到站",
                max(0, value),
                scheduled_time,
                0,
                max(0, value),
            )
        if value <= 20:
            return (
                DepartureSection.AVAILABLE,
                DepartureDecision.CAN_WAIT,
                f"約{_mins_zh(value)}分鐘後",
                "可以等",
                value,
                scheduled_time,
                10,
                value,
            )
        return (
            DepartureSection.AVAILABLE,
            DepartureDecision.LONG_WAIT,
            f"約{_mins_zh(value)}分鐘後",
            "等待較久",
            value,
            scheduled_time,
            20,
            value,
        )

    if scheduled_time:
        sort_minutes = _scheduled_minutes_from_now(scheduled_time, now)
        return (
            DepartureSection.AVAILABLE,
            DepartureDecision.SCHEDULED,
            f"預定 {scheduled_time}",
            "尚未發車",
            None,
            scheduled_time,
            30,
            sort_minutes,
        )

    return (
        DepartureSection.NOT_DEPARTED,
        DepartureDecision.NOT_DEPARTED,
        "未發車",
        "尚未發車",
        None,
        None,
        200,
        9999,
    )


# ── structured product builds (HTTP API) ──────────────────────────────────────


def _summary(routes: tuple[DepartureRouteStatus, ...]) -> DepartureSummary:
    return DepartureSummary(
        available_count=sum(
            1 for route in routes if route.section == DepartureSection.AVAILABLE
        ),
        not_departed_count=sum(
            1 for route in routes if route.section == DepartureSection.NOT_DEPARTED
        ),
        last_departed_count=sum(
            1 for route in routes if route.section == DepartureSection.LAST_DEPARTED
        ),
        unknown_count=sum(
            1 for route in routes if route.section == DepartureSection.UNKNOWN
        ),
    )


def _sort_key(route: DepartureRouteStatus) -> tuple[int, int, str, int]:
    return (route.sort_priority, route.sort_minutes, route.route, route.go_back)


def _direction_label_from_info(
    route_info: dict[str, dict],
    route: str,
    go_back: int,
) -> str:
    info = route_info.get(route, {})
    if go_back == 1:
        dest = info.get("go_dest", "")
        return f"往{dest}" if dest else "去程"
    dest = info.get("back_dest", "")
    return f"往{dest}" if dest else "回程"


def _stop_detail_from_row(stop: dict, kiosk_stop_name: str) -> RouteStopDetail | None:
    seq = _as_int(stop.get("SeqNo"))
    name = str(stop.get("StopName") or "").strip()
    if seq is None or not name:
        return None

    _, _, status_text, _, minutes, scheduled_time, _, _ = _classify_stop(
        stop, datetime.now(TAIPEI_TZ)
    )
    return RouteStopDetail(
        seq=seq,
        name=name,
        is_current_stop=kiosk_stop_name in name,
        status_text=status_text,
        minutes=minutes,
        scheduled_time=scheduled_time,
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
        if go_back is not None and stop_go_back != go_back:
            continue

        route_id = _as_int(stop.get("xno"))
        if route_id is None:
            continue

        route = route_by_id.get(route_id)
        if route is None:
            continue

        (
            section,
            decision,
            status_text,
            decision_text,
            minutes,
            scheduled_time,
            priority,
            sort_minutes,
        ) = _classify_stop(stop, now)
        direction = _direction_label_from_info(route_info, route, stop_go_back)
        dedupe_key = (route, stop_go_back, status_text)
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
                section=section,
                decision=decision,
                status_text=status_text,
                decision_text=decision_text,
                minutes=minutes,
                scheduled_time=scheduled_time,
                sort_priority=priority,
                sort_minutes=sort_minutes,
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
            stops=tuple(sorted(stops, key=lambda stop: stop.seq)),
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


# ── string renderers (LLM agent tools) ────────────────────────────────────────
#
# These collapse classification + provider into the str outputs the LLM sees.
# They share `_classify_stop` so wording never drifts from the API surface.

_SECTION_GROUP_LABEL: dict[DepartureSection, str] = {
    DepartureSection.AVAILABLE: "尚有到站資訊",
    DepartureSection.NOT_DEPARTED: "未發車",
    DepartureSection.LAST_DEPARTED: "末班駛離",
}


async def render_arrivals(
    route: str,
    stop_name: str,
    go_back: int | None = None,
) -> str:
    """Render `route` arrivals at `stop_name` as a kiosk-style string."""
    provider = get_provider()
    try:
        route_info = await provider.load_route_info(stop_name)
    except Exception as error:
        return f"雲林公車查詢失敗：{error}"

    info = route_info.get(route)
    route_id = _as_int(info.get("id")) if info is not None else None
    if route_id is None:
        return f"在 {stop_name} 找不到停靠路線 {route}"

    try:
        data = await provider.fetch_route_estimate(route_id)
    except Exception as error:
        return f"雲林公車查詢失敗：{error}"

    matches = [
        stop for stop in data
        if stop_name in stop.get("StopName", "")
        and (go_back is None or stop.get("GoBack") == go_back)
    ]
    if not matches:
        return f"路線 {route} 上找不到包含 {stop_name} 的站牌"

    now = datetime.now(TAIPEI_TZ)
    results = []
    for stop in matches:
        stop_go_back = stop.get("GoBack", 1)
        label = _direction_label_from_info(route_info, route, stop_go_back)
        _, _, status_text, _, _, _, _, _ = _classify_stop(stop, now)
        results.append(f"{label}：{status_text}")

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
    except Exception as error:
        return f"雲林公車查詢失敗：{error}"

    route_by_id = {info["id"]: name for name, info in route_info.items()}
    sections: dict[str, list[str]] = {
        "尚有到站資訊": [],
        "未發車": [],
        "末班駛離": [],
    }
    seen: set[str] = set()
    now = datetime.now(TAIPEI_TZ)

    for stop in eta_data:
        stop_go_back = stop.get("GoBack", 1)
        if go_back is not None and stop_go_back != go_back:
            continue

        try:
            route_id = int(stop["xno"])
        except (KeyError, TypeError, ValueError):
            continue

        route = route_by_id.get(route_id)
        if route is None:
            continue

        section, _, status_text, _, _, _, _, _ = _classify_stop(stop, now)
        # UNKNOWN rows are skipped — the agent's kiosk-style output only
        # surfaces the three actionable groups (matches the legacy renderer).
        group = _SECTION_GROUP_LABEL.get(section)
        if group is None:
            continue

        label = _direction_label_from_info(route_info, route, stop_go_back)
        line = f"{route} {label}：{status_text}"
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
    provider = get_provider()
    try:
        route_info = await provider.load_route_info(stop_name)
    except Exception as error:
        return f"雲林公車查詢失敗：{error}"

    info = route_info.get(route)
    route_id = _as_int(info.get("id")) if info is not None else None
    if route_id is None:
        return f"在 {stop_name} 找不到停靠路線 {route}"

    try:
        data = await provider.fetch_route_estimate(route_id)
    except Exception as error:
        return f"雲林公車查詢失敗：{error}"

    by_direction: dict[int, list[tuple[int, str]]] = {}
    for stop in data:
        go_back = stop.get("GoBack", 1)
        seq = stop.get("SeqNo", 0)
        name = _strip_paren(stop.get("StopName", ""))
        by_direction.setdefault(go_back, []).append((seq, name))

    if not by_direction:
        return f"路線 {route} 無站牌資料"

    results = []
    for go_back, stops in sorted(by_direction.items()):
        label = _direction_label_from_info(route_info, route, go_back)
        ordered = [name for _, name in sorted(stops)]
        results.append(f"{label}：{'→'.join(ordered)}")

    return "\n".join(results)


def _stops_by_direction_with_seq(
    data: list[dict],
) -> dict[int, list[tuple[int, str]]]:
    """Group route_estimate rows by GoBack, retaining (seq, stripped_name)."""
    by_direction: dict[int, list[tuple[int, str]]] = {}
    for stop in data:
        gb = stop.get("GoBack", 1)
        seq = _as_int(stop.get("SeqNo"))
        if seq is None:
            continue
        name = _strip_paren(stop.get("StopName", ""))
        by_direction.setdefault(gb, []).append((seq, name))
    return by_direction


def _name_matches(needle: str, hay: str) -> bool:
    """Substring match in either direction so '斗六' matches '斗六火車站'."""
    return needle in hay or hay in needle


def _downstream_names(
    stops: list[tuple[int, str]],
    kiosk_stop: str,
) -> list[str] | None:
    """Return stop names at or after the first kiosk-matching position.

    Returns None when kiosk doesn't appear in this direction — caller should
    skip it (the bus on this direction doesn't pass through here at all).
    Includes the kiosk itself so「有沒有停 X 」when X is the kiosk answers 有.
    """
    kiosk_seq = next(
        (s for s, n in stops if _name_matches(kiosk_stop, n)),
        None,
    )
    if kiosk_seq is None:
        return None
    return [n for s, n in stops if s >= kiosk_seq]


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
    provider = get_provider()
    try:
        route_info = await provider.load_route_info(kiosk_stop)
    except Exception as error:
        return f"雲林公車查詢失敗：{error}"

    info = route_info.get(route)
    route_id = _as_int(info.get("id")) if info is not None else None
    if route_id is None:
        return f"在 {kiosk_stop} 找不到停靠路線 {route}"

    try:
        data = await provider.fetch_route_estimate(route_id)
    except Exception as error:
        return f"雲林公車查詢失敗：{error}"

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


async def render_routes_to_destination(
    destination: str,
    kiosk_stop: str,
) -> str:
    """Find all routes at kiosk_stop that pass through destination.

    Queries all routes in parallel; matching is substring-based. Returns at
    most two matching route+direction strings so LLM output stays concise.
    """
    provider = get_provider()
    try:
        route_info = await provider.load_route_info(kiosk_stop)
    except Exception as error:
        return f"雲林公車查詢失敗：{error}"

    if not route_info:
        return f"找不到 {kiosk_stop} 的路線資訊"

    async def _check(route_name: str, route_id: int) -> list[tuple[str, str]]:
        try:
            data = await provider.fetch_route_estimate(route_id)
        except Exception:
            return []
        hits: list[tuple[str, str]] = []
        for gb, stops in _stops_by_direction_with_seq(data).items():
            downstream = _downstream_names(stops, kiosk_stop)
            if downstream is None:
                continue
            if any(_name_matches(destination, n) for n in downstream):
                label = _direction_label_from_info(route_info, route_name, gb)
                hits.append((route_name, label))
        return hits

    valid = [(name, _as_int(info.get("id"))) for name, info in route_info.items()]
    tasks = [_check(name, rid) for name, rid in valid if rid is not None]
    raw_hits = [hit for hits in await asyncio.gather(*tasks) for hit in hits]

    # Collapse same route appearing in multiple directions: direction label adds
    # no signal when all directions serve the destination.
    by_route: dict[str, list[str]] = {}
    for route_name, label in raw_hits:
        by_route.setdefault(route_name, []).append(label)
    all_hits = [
        route_name if len(labels) > 1 else f"{route_name} {labels[0]}"
        for route_name, labels in by_route.items()
    ]

    if not all_hits:
        return f"本站沒有直達{destination}的路線"
    return "、".join(all_hits[:2])


async def render_routes_at_stop(stop_name: str) -> str:
    """Render the list of routes serving `stop_name` (no ETA, no classify)."""
    provider = get_provider()
    try:
        data = await provider.fetch_routes_at_stop(stop_name)
    except Exception as error:
        return f"站名查詢失敗：{error}"

    if not data:
        return f"找不到 {stop_name} 這個站牌"

    seen: set[str] = set()
    lines: list[str] = []
    for r in data:
        name = r.get("name", "?")
        if name not in seen:
            seen.add(name)
            lines.append(name)

    return f"{stop_name} 停靠路線：\n" + "\n".join(lines)
