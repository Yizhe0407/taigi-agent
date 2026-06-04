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
_FULLWIDTH_RE = re.compile(r"[！-～]")


def _normalize_route_key(s: str) -> str:
    """Halfwidth + strip trailing 路 + uppercase for loose route lookup."""
    s = _FULLWIDTH_RE.sub(lambda m: chr(ord(m.group()) - 0xFEE0), s)
    return s.rstrip("路").upper()


def _lookup_route(route_info: dict, route: str) -> dict | None:
    """Case-insensitive route lookup that ignores trailing 路 and fullwidth."""
    key = _normalize_route_key(route)
    for name, info in route_info.items():
        if _normalize_route_key(name) == key:
            return info
    return None


def _strip_paren(name: str) -> str:
    """Remove parenthetical suffixes from stop names: 持法媽祖宮(頂溪) → 持法媽祖宮."""
    return _PAREN_RE.sub("", name).strip()


def _mins_zh(n: int) -> str:
    """Integer minutes → natural Chinese count."""
    if n <= 0:
        return "零"
    if n < 10:
        return _ONES_ZH[n]
    if n < 20:
        return "十" + _ONES_ZH[n % 10]
    if n < 100:
        return _ONES_ZH[n // 10] + "十" + _ONES_ZH[n % 10]
    return str(n)  # 100分鐘以上直接用數字


def _fmt_time_12h(hhmm: str) -> str:
    """'HH:MM' → '上午/下午X點Y分'（TTS 友善）。"""
    h, m = map(int, hhmm.split(":"))
    period = "上午" if h < 12 else "下午"
    h12 = h % 12 or 12
    return f"{period}{h12}點" if m == 0 else f"{period}{h12}點{m}分"


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


@dataclass(frozen=True)
class StopClassification:
    section: DepartureSection
    decision: DepartureDecision
    status_text: str
    decision_text: str
    minutes: int | None
    scheduled_time: str | None
    sort_priority: int
    sort_minutes: int


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


def _classify_stop(stop: dict, now: datetime) -> StopClassification:
    """Classify a bus ETA row into a user-facing departure decision.

    ``minutes`` is the real-time ETA shown to the user (None when unavailable).
    ``sort_minutes`` is always an int (minutes-from-now) used only for sorting;
    scheduled-only rows derive this from ComeTime so the list stays time-ordered
    across midnight boundaries.
    """
    raw_value = stop.get("Value")
    value = _as_int(raw_value)
    scheduled_time = str(stop.get("ComeTime") or "").strip() or None

    if raw_value is not None and value is None:
        return StopClassification(
            DepartureSection.UNKNOWN, DepartureDecision.UNKNOWN,
            "狀態不明", "資料異常", None, scheduled_time, 400, 9999,
        )

    if value == -3:
        return StopClassification(
            DepartureSection.LAST_DEPARTED, DepartureDecision.LAST_DEPARTED,
            "末班駛離", "末班已過", None, scheduled_time, 300, 9999,
        )

    if value is not None:
        if value < 0:
            return StopClassification(
                DepartureSection.UNKNOWN, DepartureDecision.UNKNOWN,
                "狀態不明", "資料異常", None, scheduled_time, 400, 9999,
            )
        if value <= 3:
            return StopClassification(
                DepartureSection.AVAILABLE, DepartureDecision.ARRIVING_SOON,
                "即將到站", "即將到站", max(0, value), scheduled_time, 0, max(0, value),
            )
        if value <= 20:
            return StopClassification(
                DepartureSection.AVAILABLE, DepartureDecision.CAN_WAIT,
                f"約{_mins_zh(value)}分鐘後", "可以等", value, scheduled_time, 10, value,
            )
        return StopClassification(
            DepartureSection.AVAILABLE, DepartureDecision.LONG_WAIT,
            f"約{_mins_zh(value)}分鐘後", "等待較久", value, scheduled_time, 20, value,
        )

    if scheduled_time:
        return StopClassification(
            DepartureSection.AVAILABLE, DepartureDecision.SCHEDULED,
            f"{_fmt_time_12h(scheduled_time)}發車", "尚未發車", None, scheduled_time,
            30, _scheduled_minutes_from_now(scheduled_time, now),
        )

    return StopClassification(
        DepartureSection.NOT_DEPARTED, DepartureDecision.NOT_DEPARTED,
        "未發車", "尚未發車", None, None, 200, 9999,
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


def _is_terminal_direction(
    stop_name: str,
    route_info: dict[str, dict],
    route: str,
    go_back: int,
) -> bool:
    """True when this stop is the non-circular terminus for this direction.

    Circular routes (go_dest == back_dest == stop) always return False so
    they are shown — the bus departs from here even though it also returns here.
    """
    info = route_info.get(route, {})
    go_dest = info.get("go_dest", "")
    back_dest = info.get("back_dest", "")
    is_circular = _name_matches(stop_name, go_dest) and _name_matches(stop_name, back_dest)
    if is_circular:
        return False
    terminus = go_dest if go_back == 1 else back_dest
    return _name_matches(stop_name, terminus)


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
            # Admin set an explicit direction — honour it without auto-detect.
            if stop_go_back != go_back:
                continue
        else:
            # Auto-detect: skip directions where this stop is the non-circular
            # terminus (bus is arriving/terminating here, not departing).
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
    DepartureSection.AVAILABLE: "有車",
    DepartureSection.NOT_DEPARTED: "尚未發車",
    DepartureSection.LAST_DEPARTED: "末班已過",
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
        return "查詢失敗，請稍後再試。"

    info = _lookup_route(route_info, route)
    route_id = _as_int(info.get("id")) if info is not None else None
    if route_id is None:
        return f"本站沒有路線 {route}。"

    try:
        data = await provider.fetch_route_estimate(route_id)
    except Exception as error:
        return "查詢失敗，請稍後再試。"

    matches = [
        stop for stop in data
        if stop_name in stop.get("StopName", "")
        and (go_back is None or stop.get("GoBack") == go_back)
    ]
    if not matches:
        return f"路線 {route} 不停 {stop_name}。"

    now = datetime.now(TAIPEI_TZ)
    results = []
    for stop in matches:
        stop_go_back = stop.get("GoBack", 1)
        status_text = _classify_stop(stop, now).status_text
        if status_text.endswith("後"):
            status_text = status_text + "來車"
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
    except Exception as error:
        return "查詢失敗，請稍後再試。"

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
    provider = get_provider()
    try:
        route_info = await provider.load_route_info(stop_name)
    except Exception as error:
        return "查詢失敗，請稍後再試。"

    info = route_info.get(route)
    route_id = _as_int(info.get("id")) if info is not None else None
    if route_id is None:
        return f"本站沒有路線 {route}。"

    try:
        data = await provider.fetch_route_estimate(route_id)
    except Exception as error:
        return "查詢失敗，請稍後再試。"

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


_STOP_SUFFIX = frozenset("站路街號市區鄉鎮村里")


def _stop_similarity(a: str, b: str) -> float:
    """Character-set union + bigram Jaccard similarity for Chinese station names.

    Strips common geographic suffixes so '雲林高鐵站' vs '高鐵雲林站' scores 1.0
    (identical character sets) and auto-remaps without LLM intervention.
    """
    a_core = [c for c in a if c not in _STOP_SUFFIX]
    b_core = [c for c in b if c not in _STOP_SUFFIX]
    if not a_core or not b_core:
        return 0.0
    a_set, b_set = set(a_core), set(b_core)
    char_ratio = len(a_set & b_set) / len(a_set | b_set)
    a_bi = {a_core[i] + a_core[i + 1] for i in range(len(a_core) - 1)}
    b_bi = {b_core[i] + b_core[i + 1] for i in range(len(b_core) - 1)}
    bi_ratio = len(a_bi & b_bi) / len(a_bi | b_bi) if (a_bi or b_bi) else 0.0
    return max(char_ratio, bi_ratio)


def _fuzzy_candidates(destination: str, stop_names: set[str]) -> list[tuple[str, float]]:
    """Return (name, score) pairs sorted by similarity, score > 0.35 only."""
    scored = [
        (name, _stop_similarity(destination, name))
        for name in stop_names
        if name != destination
    ]
    scored.sort(key=lambda x: -x[1])
    return [(name, score) for name, score in scored if score > 0.35]


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
        return "查詢失敗，請稍後再試。"

    info = route_info.get(route)
    route_id = _as_int(info.get("id")) if info is not None else None
    if route_id is None:
        return f"在 {kiosk_stop} 找不到停靠路線 {route}"

    try:
        data = await provider.fetch_route_estimate(route_id)
    except Exception as error:
        return "查詢失敗，請稍後再試。"

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

    When destination matches nothing exactly, fuzzy similarity is applied:
    - score >= 0.8: auto-remap silently (e.g. '雲林高鐵站' → '高鐵雲林站')
    - 0.35 <= score < 0.8: return candidate hint so LLM can retry or clarify
    """
    provider = get_provider()
    try:
        route_info = await provider.load_route_info(kiosk_stop)
    except Exception as error:
        return "查詢失敗，請稍後再試。"

    if not route_info:
        return "查詢失敗，請稍後再試。"

    now = datetime.now(TAIPEI_TZ)

    async def _check(
        route_name: str, route_id: int
    ) -> tuple[list[tuple[str, int]], set[str]]:
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

            kiosk_rows = [
                row for row in data
                if kiosk_stop in row.get("StopName", "")
                and row.get("GoBack", 1) == gb
            ]
            if kiosk_rows:
                c = _classify_stop(kiosk_rows[0], now)
                status_text = c.status_text
                # Append "來車" to relative-time texts so LLM knows this is the bus
                # arriving at the kiosk, not the travel time to the destination.
                if status_text.endswith("後"):
                    status_text = status_text + "來車"
                hits.append((f"{route_name} {direction}：{status_text}", c.sort_minutes))
            else:
                hits.append((f"{route_name} {direction}：無即時資料", 9999))

        return hits, all_downstream

    valid = [(name, _as_int(info.get("id"))) for name, info in route_info.items()]
    tasks = [_check(name, rid) for name, rid in valid if rid is not None]
    results = await asyncio.gather(*tasks)
    raw = [item for hits, _ in results for item in hits]
    all_stops = {name for _, stops in results for name in stops}

    if not raw:
        if _allow_remap:
            candidates = _fuzzy_candidates(destination, all_stops)
            if candidates:
                best_name, best_score = candidates[0]
                if best_score >= 0.8:
                    return await render_arrivals_to_destination(
                        best_name, kiosk_stop, go_back, _allow_remap=False
                    )
                top = "、".join(name for name, _ in candidates[:3])
                return f"查無「{destination}」站名。本站路線鄰近站名：{top}。"
        return f"本站沒有直達{destination}的路線"

    raw.sort(key=lambda x: x[1])

    # Return all results sorted by arrival time. The LLM decides how many to
    # surface based on conversation context — first query gets the fastest,
    # follow-up "下一班" uses the second entry already in the history, etc.
    # Routes with no real-time data appear last so the LLM can still explain status.
    real_time = [(d, m) for d, m in raw if m < 9999]
    if real_time:
        return "\n".join(d for d, _ in real_time)
    return "\n".join(d for d, _ in raw)


async def render_routes_at_stop(stop_name: str) -> str:
    """Render the list of routes serving `stop_name` (no ETA, no classify)."""
    provider = get_provider()
    try:
        data = await provider.fetch_routes_at_stop(stop_name)
    except Exception as error:
        return "查詢失敗，請稍後再試。"

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
