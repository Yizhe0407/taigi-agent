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


def _unknown(scheduled_time: str | None) -> StopClassification:
    """Malformed/negative ETA row — surfaced as 狀態不明, always sorts last."""
    return StopClassification(
        section=DepartureSection.UNKNOWN,
        decision=DepartureDecision.UNKNOWN,
        status_text="狀態不明",
        decision_text="資料異常",
        minutes=None,
        scheduled_time=scheduled_time,
        sort_priority=400,
        sort_minutes=9999,
    )


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
        return _unknown(scheduled_time)

    if value == -3:
        return StopClassification(
            section=DepartureSection.LAST_DEPARTED,
            decision=DepartureDecision.LAST_DEPARTED,
            status_text="末班駛離",
            decision_text="末班已過",
            minutes=None,
            scheduled_time=scheduled_time,
            sort_priority=300,
            sort_minutes=9999,
        )

    if value is not None:
        if value < 0:
            return _unknown(scheduled_time)
        if value <= 3:
            return StopClassification(
                section=DepartureSection.AVAILABLE,
                decision=DepartureDecision.ARRIVING_SOON,
                status_text="即將到站",
                decision_text="即將到站",
                minutes=max(0, value),
                scheduled_time=scheduled_time,
                sort_priority=0,
                sort_minutes=max(0, value),
            )
        if value <= 20:
            return StopClassification(
                section=DepartureSection.AVAILABLE,
                decision=DepartureDecision.CAN_WAIT,
                status_text=f"約{_mins_zh(value)}分鐘後",
                decision_text="可以等",
                minutes=value,
                scheduled_time=scheduled_time,
                sort_priority=10,
                sort_minutes=value,
            )
        return StopClassification(
            section=DepartureSection.AVAILABLE,
            decision=DepartureDecision.LONG_WAIT,
            status_text=f"約{_mins_zh(value)}分鐘後",
            decision_text="等待較久",
            minutes=value,
            scheduled_time=scheduled_time,
            sort_priority=20,
            sort_minutes=value,
        )

    if scheduled_time:
        return StopClassification(
            section=DepartureSection.AVAILABLE,
            decision=DepartureDecision.SCHEDULED,
            status_text=f"{_fmt_time_12h(scheduled_time)} 發車",
            decision_text="尚未發車",
            minutes=None,
            scheduled_time=scheduled_time,
            sort_priority=30,
            sort_minutes=_scheduled_minutes_from_now(scheduled_time, now),
        )

    return StopClassification(
        section=DepartureSection.NOT_DEPARTED,
        decision=DepartureDecision.NOT_DEPARTED,
        status_text="未發車",
        decision_text="尚未發車",
        minutes=None,
        scheduled_time=None,
        sort_priority=200,
        sort_minutes=9999,
    )
