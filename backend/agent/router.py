"""Deterministic intent router for the kiosk bus agent.

Classifies user input into one of N intents using regex/keyword rules —
no LLM call. Each `Decision` is one of:
  * canned_response — reply verbatim, no LLM round-trip
  * tool_call      — deterministic tool call, then phrasing
  * fallback_to_llm — hand back to legacy LLM loop

Replaces prompt-as-state-machine. The 4B model is unreliable at running
a 10-rule decision tree from a system prompt; Python regex is
deterministic and ~free. Intents are migrated into the router
incrementally; UNKNOWN safely falls back to the existing LLM path so
each migration step is independently verifiable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class Intent(Enum):
    """Each enum value names a kiosk-bus decision-rule outcome.

    The first three are handled in the router today; the rest are
    placeholders for incremental migration and currently fall back
    to the LLM path.
    """

    ROUTE_ONLY = "route_only"
    REMOTE_DESTINATION = "remote_destination"
    TIMETABLE_UNSUPPORTED = "timetable_unsupported"
    # Reserved — not yet handled by router; will be migrated in Cut 2.2+.
    FIND_ROUTES_TO_DEST = "find_routes_to_dest"
    OTHER_ROUTES_FOLLOWUP = "other_routes_followup"
    ROUTES_AT_STOP = "routes_at_stop"
    STOP_STATUS = "stop_status"
    ARRIVAL_TIME = "arrival_time"
    ROUTE_STOPS_CLARIFY = "route_stops_clarify"
    CHECK_STOP_ON_ROUTE = "check_stop_on_route"
    ASK_ROUTE_NUMBER = "ask_route_number"
    OFF_TOPIC = "off_topic"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ConvState:
    """Typed conversation state, computed after each turn.

    Lets follow-up intents (「那還有其他路線嗎」, 「幾點有車」after
    a route has been confirmed) resolve deterministically instead of
    asking the LLM to recall context from messages.
    """

    last_route: str | None = None
    last_destination: str | None = None
    last_intent: Intent | None = None
    # Rule 8 pending: route number for which we asked
    # 「去程還是回程？」and are awaiting an answer.
    pending_stops_clarify_route: str | None = None


@dataclass(frozen=True)
class Decision:
    """Outcome of `IntentRouter.classify`.

    Exactly one of `canned_response`, `tool_call`, or `fallback_to_llm`
    is set. `next_state` (when not None) replaces the session's
    ConvState after this turn.
    """

    intent: Intent
    canned_response: str | None = None
    tool_call: tuple[str, dict] | None = None
    fallback_to_llm: bool = False
    next_state: ConvState | None = None


# ── Patterns ─────────────────────────────────────────────────────────────────

# Pure route number, optionally suffixed with 路. Examples: 201 / 201路 /
# 7000b / Y01. Bounded digit count prevents matching arbitrary numbers like
# years or amounts.
_ROUTE_ONLY_RE = re.compile(r"^([A-Za-z]?\d{2,4}[A-Za-z]?)路?$")

# Remote (cross-county) destinations the kiosk cannot route to directly —
# user must use a map planner instead.
_REMOTE_CITIES = (
    "台北", "臺北", "台中", "臺中", "高雄", "嘉義", "彰化", "南投",
    "新北", "桃園", "新竹", "宜蘭", "花蓮", "台東", "臺東", "屏東",
)

# Timetable / inter-stop travel-time queries the kiosk doesn't support.
# Carefully avoids matching real-time arrival phrasings like
# 「幾點有車」「幾點來」「下一班幾點」which belong to ARRIVAL_TIME.
_TIMETABLE_RE = re.compile(
    r"(完整時刻表|全天時刻表|時刻表|班次表"
    r"|幾點幾分發車|發車時刻"
    r"|站間.{0,10}幾分鐘"
    r"|從.{1,20}到.{1,20}(要|大概|大約)?.{0,5}幾分鐘?)"
)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _to_halfwidth(s: str) -> str:
    """Convert full-width ASCII to half-width so regex matches consistently."""
    return "".join(
        chr(ord(c) - 0xFEE0) if 0xFF01 <= ord(c) <= 0xFF5E else c
        for c in s
    )


def _is_remote_destination(text: str) -> bool:
    """True when input names a remote city or asks about 轉乘.

    「轉乘」 by itself implies a multi-leg trip the kiosk can't plan, so it
    triggers the same redirect regardless of which destination is named.
    """
    if "轉乘" in text:
        return True
    return any(city in text for city in _REMOTE_CITIES)


# ── Router ───────────────────────────────────────────────────────────────────


class IntentRouter:
    """Deterministic classifier: user_input + ConvState -> Decision."""

    def classify(self, user_input: str, state: ConvState) -> Decision:
        text = _to_halfwidth(user_input).strip()
        if not text:
            return Decision(intent=Intent.UNKNOWN, fallback_to_llm=True)

        # Rule 1: pure route number → ask the user what about it.
        # Highest priority — even Rule 2 keywords matter less than "user
        # just said a route number with no question". Echoes the number back
        # so the next turn can resolve via state.last_route.
        match = _ROUTE_ONLY_RE.match(text)
        if match:
            route = match.group(1)
            return Decision(
                intent=Intent.ROUTE_ONLY,
                canned_response=(
                    f"{route}您想查什麼，到站時間還是有沒有停某個地方？"
                ),
                next_state=ConvState(
                    last_route=route,
                    last_destination=state.last_destination,
                    last_intent=Intent.ROUTE_ONLY,
                ),
            )

        # Rule 2: remote destination / transfer → map redirect.
        if _is_remote_destination(text):
            return Decision(
                intent=Intent.REMOTE_DESTINATION,
                canned_response="這段要用地圖規劃比較準喔。",
                next_state=ConvState(
                    last_route=state.last_route,
                    last_destination=state.last_destination,
                    last_intent=Intent.REMOTE_DESTINATION,
                ),
            )

        # Rule 3: timetable / inter-stop ETA → unsupported, offer alternative.
        if _TIMETABLE_RE.search(text):
            return Decision(
                intent=Intent.TIMETABLE_UNSUPPORTED,
                canned_response="時刻表查不了，要查到站時間嗎？",
                next_state=ConvState(
                    last_route=state.last_route,
                    last_destination=state.last_destination,
                    last_intent=Intent.TIMETABLE_UNSUPPORTED,
                ),
            )

        # Everything else → legacy LLM loop.
        return Decision(intent=Intent.UNKNOWN, fallback_to_llm=True)
