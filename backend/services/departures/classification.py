from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from services.departures.normalize import _mins_zh


class DepartureSection(StrEnum):
    AVAILABLE = "available"
    NOT_DEPARTED = "not_departed"
    LAST_DEPARTED = "last_departed"
    UNKNOWN = "unknown"


class DepartureDecision(StrEnum):
    ARRIVING_SOON = "arriving_soon"
    CAN_WAIT = "can_wait"
    LONG_WAIT = "long_wait"
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
    scheduled_time: str | None  # HH:MM of next scheduled departure (ebus ComeTime); None for TDX
    sort_priority: int
    sort_minutes: int


# Arrival-time thresholds (minutes) that split AVAILABLE into ARRIVING_SOON /
# CAN_WAIT / LONG_WAIT.
_ARRIVING_SOON_MAX_MIN = 3
_CAN_WAIT_MAX_MIN = 20

# sort_priority tiers: lower sorts first within a stop's route list. Gaps
# between tiers leave room for future in-between states without renumbering.
_SORT_PRIORITY_ARRIVING_SOON = 0
_SORT_PRIORITY_CAN_WAIT = 10
_SORT_PRIORITY_LONG_WAIT = 20
_SORT_PRIORITY_NOT_DEPARTED = 200
_SORT_PRIORITY_LAST_DEPARTED = 300
_SORT_PRIORITY_UNKNOWN = 400


def _unknown() -> StopClassification:
    return StopClassification(
        section=DepartureSection.UNKNOWN,
        decision=DepartureDecision.UNKNOWN,
        status_text="狀態不明",
        decision_text="資料異常",
        minutes=None,
        scheduled_time=None,
        sort_priority=_SORT_PRIORITY_UNKNOWN,
        sort_minutes=9999,
    )


def _classify_stop(stop: dict, now: datetime) -> StopClassification:
    """Classify a TDX ETA row into a user-facing departure decision.

    TDX StopStatus values:
      0 = 正常（有預估到站時間）
      1 = 尚未發車
      2 = 交管不停靠  (caller must skip via `normalize._is_traffic_controlled`
          before calling here — every facade in renderers.py/snapshot.py does)
      3 = 末班車已過
      4 = 今日未營運
    """
    stop_status = stop.get("stop_status")
    estimate_seconds = stop.get("estimate_seconds")

    if stop_status == 3:
        return StopClassification(
            section=DepartureSection.LAST_DEPARTED,
            decision=DepartureDecision.LAST_DEPARTED,
            status_text="末班駛離",
            decision_text="末班已過",
            minutes=None,
            scheduled_time=None,
            sort_priority=_SORT_PRIORITY_LAST_DEPARTED,
            sort_minutes=9999,
        )

    if stop_status == 4:
        return StopClassification(
            section=DepartureSection.UNKNOWN,
            decision=DepartureDecision.UNKNOWN,
            status_text="今日未營運",
            decision_text="今日未營運",
            minutes=None,
            scheduled_time=None,
            sort_priority=_SORT_PRIORITY_UNKNOWN,
            sort_minutes=9999,
        )

    if stop_status == 1:
        scheduled_time = stop.get("scheduled_time")
        sched_minutes: int | None = None
        sort_mins = 9999
        if scheduled_time:
            try:
                h, m = map(int, scheduled_time.split(":"))
                target = now.replace(hour=h, minute=m, second=0, microsecond=0)
                diff = round((target - now).total_seconds() / 60)
                # diff < 0 near midnight (now=23:50, scheduled=00:10) is
                # ambiguous with stale same-day data (now=08:00, scheduled=07:00)
                # without knowing the service day boundary — silently drop the
                # minute count rather than guess; decision_text still shows 未發車.
                if diff >= 0:
                    sched_minutes = diff
                    sort_mins = diff
            except (ValueError, AttributeError):
                pass
        return StopClassification(
            section=DepartureSection.NOT_DEPARTED,
            decision=DepartureDecision.NOT_DEPARTED,
            status_text="未發車",
            decision_text="尚未發車",
            minutes=sched_minutes,
            scheduled_time=scheduled_time,
            sort_priority=_SORT_PRIORITY_NOT_DEPARTED,
            sort_minutes=sort_mins,
        )

    if stop_status == 0 and estimate_seconds is not None:
        minutes = estimate_seconds // 60
        if minutes <= _ARRIVING_SOON_MAX_MIN:
            return StopClassification(
                section=DepartureSection.AVAILABLE,
                decision=DepartureDecision.ARRIVING_SOON,
                status_text="即將到站",
                decision_text="即將到站",
                minutes=max(0, minutes),
                scheduled_time=None,
                sort_priority=_SORT_PRIORITY_ARRIVING_SOON,
                sort_minutes=max(0, minutes),
            )
        if minutes <= _CAN_WAIT_MAX_MIN:
            return StopClassification(
                section=DepartureSection.AVAILABLE,
                decision=DepartureDecision.CAN_WAIT,
                status_text=f"約{_mins_zh(minutes)}分鐘後",
                decision_text="可以等",
                minutes=minutes,
                scheduled_time=None,
                sort_priority=_SORT_PRIORITY_CAN_WAIT,
                sort_minutes=minutes,
            )
        return StopClassification(
            section=DepartureSection.AVAILABLE,
            decision=DepartureDecision.LONG_WAIT,
            status_text=f"約{_mins_zh(minutes)}分鐘後",
            decision_text="等待較久",
            minutes=minutes,
            scheduled_time=None,
            sort_priority=_SORT_PRIORITY_LONG_WAIT,
            sort_minutes=minutes,
        )

    return _unknown()
