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

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from zoneinfo import ZoneInfo

from providers.bus import BusProvider
from providers.yunlin_ebus import YunlinEbusProvider

TAIPEI_TZ = ZoneInfo("Asia/Taipei")


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
                f"約 {value} 分鐘後",
                "可以等",
                value,
                scheduled_time,
                10,
                value,
            )
        return (
            DepartureSection.AVAILABLE,
            DepartureDecision.LONG_WAIT,
            f"約 {value} 分鐘後",
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


def build_departure_snapshot(
    stop_name: str,
    go_back: int | None = None,
    *,
    updated_at: datetime | None = None,
) -> StopDepartureSnapshot:
    """Build a structured departure snapshot for one stop.

    Deterministic product layer: converts raw bus ETA rows into stable
    sections and decisions that the dashboard, TTS, and avatar can share.
    """
    now = datetime.now(TAIPEI_TZ)
    provider = get_provider()

    try:
        eta_data = provider.fetch_eta_at_stop(stop_name)
        route_info = provider.load_route_info(stop_name)
    except Exception as error:
        raise DepartureSnapshotUnavailable(f"雲林公車查詢失敗：{error}") from error

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
        direction = provider.direction_label(route, stop_name, stop_go_back)
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


def build_route_detail(
    route: str,
    stop_name: str,
    go_back: int | None = None,
) -> DepartureRouteDetail:
    """Build structured stop-order details for a route serving one kiosk stop."""
    provider = get_provider()
    try:
        route_info = provider.load_route_info(stop_name)
    except Exception as error:
        raise RouteDetailUnavailable(f"雲林公車查詢失敗：{error}") from error

    info = route_info.get(route)
    if info is None:
        raise RouteDetailNotFound(f"在「{stop_name}」找不到停靠路線 {route}")

    route_id = _as_int(info.get("id"))
    if route_id is None:
        raise RouteDetailUnavailable(f"路線 {route} 的 route id 格式異常")

    try:
        estimate_data = provider.fetch_route_estimate(route_id)
    except Exception as error:
        raise RouteDetailUnavailable(f"雲林公車查詢失敗：{error}") from error

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
            label=provider.direction_label(route, stop_name, row_go_back),
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


def render_arrivals(
    route: str,
    stop_name: str,
    go_back: int | None = None,
) -> str:
    """Render `route` arrivals at `stop_name` as a kiosk-style string."""
    provider = get_provider()
    try:
        route_id = provider.get_route_id(route, stop_name)
    except Exception as error:
        return f"雲林公車查詢失敗：{error}"

    if route_id is None:
        return f"在「{stop_name}」找不到停靠路線 {route}"

    try:
        data = provider.fetch_route_estimate(route_id)
    except Exception as error:
        return f"雲林公車查詢失敗：{error}"

    matches = [
        stop for stop in data
        if stop_name in stop.get("StopName", "")
        and (go_back is None or stop.get("GoBack") == go_back)
    ]
    if not matches:
        return f"路線 {route} 上找不到包含「{stop_name}」的站牌"

    now = datetime.now(TAIPEI_TZ)
    results = []
    for stop in matches:
        stop_go_back = stop.get("GoBack", 1)
        label = provider.direction_label(route, stop_name, stop_go_back)
        _, _, status_text, _, _, _, _, _ = _classify_stop(stop, now)
        results.append(f"{label}：{status_text}")

    return "\n".join(results)


def render_stop_arrival_statuses(
    stop_name: str,
    go_back: int | None = None,
) -> str:
    """Render every route currently serving `stop_name` grouped by section."""
    provider = get_provider()
    try:
        eta_data = provider.fetch_eta_at_stop(stop_name)
        route_info = provider.load_route_info(stop_name)
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

        label = provider.direction_label(route, stop_name, stop_go_back)
        line = f"{route} {label}：{status_text}"
        if line in seen:
            continue
        seen.add(line)
        sections[group].append(line)

    if not seen:
        return f"「{stop_name}」目前無到站狀態資料"

    results = [f"「{stop_name}」目前到站狀態："]
    for title, lines in sections.items():
        if lines:
            results.append(f"{title}：")
            results.extend(lines)

    return "\n".join(results)


def render_route_stops(route: str, stop_name: str) -> str:
    """Render the full stop sequence (both directions) of `route`."""
    provider = get_provider()
    try:
        route_id = provider.get_route_id(route, stop_name)
    except Exception as error:
        return f"雲林公車查詢失敗：{error}"

    if route_id is None:
        return f"在「{stop_name}」找不到停靠路線 {route}"

    try:
        data = provider.fetch_route_estimate(route_id)
    except Exception as error:
        return f"雲林公車查詢失敗：{error}"

    by_direction: dict[int, list[tuple[int, str]]] = {}
    for stop in data:
        go_back = stop.get("GoBack", 1)
        seq = stop.get("SeqNo", 0)
        name = stop.get("StopName", "")
        by_direction.setdefault(go_back, []).append((seq, name))

    if not by_direction:
        return f"路線 {route} 無站牌資料"

    results = []
    for go_back, stops in sorted(by_direction.items()):
        label = provider.direction_label(route, stop_name, go_back)
        ordered = [name for _, name in sorted(stops)]
        results.append(f"{label}：{'→'.join(ordered)}")

    return "\n".join(results)


def render_routes_at_stop(stop_name: str) -> str:
    """Render the list of routes serving `stop_name` (no ETA, no classify)."""
    provider = get_provider()
    try:
        data = provider.fetch_routes_at_stop(stop_name)
    except Exception as error:
        return f"站名查詢失敗：{error}"

    if not data:
        return f"找不到「{stop_name}」這個站牌"

    seen: set[str] = set()
    lines: list[str] = []
    for r in data:
        name = r.get("name", "?")
        if name not in seen:
            seen.add(name)
            desc = r.get("ddesc", "")
            lines.append(f"{name}（{desc}）")

    return f"「{stop_name}」停靠路線：\n" + "\n".join(lines)
