from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from services.departures.normalize import _as_int, _fmt_time_12h, _mins_zh


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
        return StopClassification(
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
            return StopClassification(
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
            return StopClassification(
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
            return StopClassification(
                DepartureSection.AVAILABLE,
                DepartureDecision.CAN_WAIT,
                f"約{_mins_zh(value)}分鐘後",
                "可以等",
                value,
                scheduled_time,
                10,
                value,
            )
        return StopClassification(
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
        return StopClassification(
            DepartureSection.AVAILABLE,
            DepartureDecision.SCHEDULED,
            f"{_fmt_time_12h(scheduled_time)} 發車",
            "尚未發車",
            None,
            scheduled_time,
            30,
            _scheduled_minutes_from_now(scheduled_time, now),
        )

    return StopClassification(
        DepartureSection.NOT_DEPARTED,
        DepartureDecision.NOT_DEPARTED,
        "未發車",
        "尚未發車",
        None,
        None,
        200,
        9999,
    )
