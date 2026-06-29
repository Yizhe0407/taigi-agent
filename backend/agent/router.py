"""Deterministic intent router for the kiosk bus agent.

Classifies user input into one of N intents using regex/keyword rules —
no LLM call. Handles only canned-response intents (Rules 1-3); all tool
dispatch is handled by the LLM (Groq qwen3-32b). UNKNOWN falls through
to the LLM loop.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from enum import Enum

from pipeline.normalize import to_halfwidth
from tools.intent_rules import TIMETABLE_CANNED_RESPONSE, TIMETABLE_RE


class Intent(Enum):
    """Each enum value names a kiosk-bus decision-rule outcome."""

    ROUTE_ONLY = "route_only"
    REMOTE_DESTINATION = "remote_destination"
    TIMETABLE_UNSUPPORTED = "timetable_unsupported"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ConvState:
    """Typed conversation state, computed after each turn.

    Preserved across turns so canned-response rules (Rules 1-3) can
    carry forward context without touching LLM messages.
    """

    last_route: str | None = None
    last_destination: str | None = None
    last_intent: Intent | None = None


@dataclass(frozen=True)
class Decision:
    """Outcome of `IntentRouter.classify`.

    Exactly one of `canned_response` or `fallback_to_llm` is set: the router
    only resolves canned-response intents, and everything else (including all
    tool dispatch) falls through to the LLM. `next_state` (when not None)
    replaces the session's ConvState after this turn.
    """

    intent: Intent
    canned_response: str | None = None
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
    "台北",
    "臺北",
    "台中",
    "臺中",
    "高雄",
    "嘉義",
    "彰化",
    "南投",
    "新北",
    "桃園",
    "新竹",
    "宜蘭",
    "花蓮",
    "台東",
    "臺東",
    "屏東",
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
        text = to_halfwidth(user_input).strip()
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
                canned_response=(f"{route}您想查什麼，到站時間還是有沒有停某個地方？"),
                next_state=replace(state, last_route=route, last_intent=Intent.ROUTE_ONLY),
            )

        # Rule 2: remote destination / transfer → map redirect.
        if _is_remote_destination(text):
            return Decision(
                intent=Intent.REMOTE_DESTINATION,
                canned_response="這段要用地圖規劃比較準喔。",
                next_state=replace(state, last_intent=Intent.REMOTE_DESTINATION),
            )

        # Rule 3: timetable / inter-stop ETA → unsupported, offer alternative.
        if TIMETABLE_RE.search(text):
            return Decision(
                intent=Intent.TIMETABLE_UNSUPPORTED,
                canned_response=TIMETABLE_CANNED_RESPONSE,
                next_state=replace(state, last_intent=Intent.TIMETABLE_UNSUPPORTED),
            )

        # Everything else → LLM loop (tool dispatch handled by LLM).
        return Decision(intent=Intent.UNKNOWN, fallback_to_llm=True)
