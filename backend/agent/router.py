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
    """Each enum value names a kiosk-bus decision-rule outcome."""

    ROUTE_ONLY = "route_only"
    REMOTE_DESTINATION = "remote_destination"
    TIMETABLE_UNSUPPORTED = "timetable_unsupported"
    FIND_ROUTES_TO_DEST = "find_routes_to_dest"
    OTHER_ROUTES_FOLLOWUP = "other_routes_followup"
    ROUTES_AT_STOP = "routes_at_stop"
    STOP_STATUS = "stop_status"
    ARRIVAL_TIME = "arrival_time"
    ROUTE_STOPS_CLARIFY = "route_stops_clarify"
    CHECK_STOP_ON_ROUTE = "check_stop_on_route"
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

# Route number pattern.  Uses lookahead/lookbehind instead of \b because
# Python 3 treats CJK characters as \w, making \b ineffective next to them.
_ROUTE_RE = re.compile(
    r"(?<![A-Za-z\d])([A-Za-z]?\d{2,4}[A-Za-z]?)(?![A-Za-z\d])"
    r"(?!\s*(?:大樓|號出口|出口|樓層|樓|棟|館|分鐘|分|秒|公里|公尺|元|歲))"
)

# Real-time arrival question words (NOT schedule/timetable keywords).
_ARRIVAL_QUESTION_RE = re.compile(
    r"幾點(?:有車|到|來)|幾分鐘?(?:後)?(?:有|來|到)|下一班|多久(?:後)?(?:有|來|到)"
    r"|還有幾分|還有多久|快到了|到了嗎|有沒有車"
)

# Movement-intent destination extraction.  Three alternatives capture into
# group 1, 2, or 3 respectively.  Rule 2 (REMOTE_DESTINATION) runs first so
# cross-county cities are already handled before we get here.
#
# _DEST_SUFFIX is a positive lookahead — not consumed — that tells the
# non-greedy quantifier where the destination name ends.  Non-greedy `{2,10}?`
# ensures the shortest match that satisfies the lookahead, so "去虎尾的車怎麼搭"
# yields dest="虎尾" (stops at "的車") rather than "虎尾的車".
# "的車" in the suffix catches "X的車怎麼搭" phrasings.
# "要搭什麼|要怎麼搭" catches "到X要搭什麼車" phrasings.
_DEST_CHARS = r"[^\s，。？！幾哪什怎搭了嗎呢吧啊]"
_DEST_SUFFIX = r"(?=有公車|有車|的車|怎麼搭|要搭什麼|要怎麼搭|搭什麼|可以嗎|$)"
_DEST_RE = re.compile(
    rf"(?:(?:想|要)(?:去|到)|去|前往)({_DEST_CHARS}{{2,10}}?){_DEST_SUFFIX}"
    rf"|到({_DEST_CHARS}{{2,10}}?)(?=怎麼搭|要搭什麼|要怎麼搭|搭什麼|有公車|有車|可以嗎)"
    rf"|怎麼(?:去|到)({_DEST_CHARS}{{2,10}}?){_DEST_SUFFIX}"
)

# Follow-up: "還有其他路線嗎", "其他路線呢", "有沒有其他路線", "還有哪台車".
# Plain "還有車嗎" (→ STOP_STATUS) is excluded by requiring either the
# 其他/別的 qualifier OR the "哪台/哪班" phrasing that implies route enumeration.
_OTHER_ROUTES_RE = re.compile(
    r"還有(?:其他|別的)(?:路線|公車|班次|車)"
    r"|還有哪(?:台|班|路)?(?:車|路線|公車)"
    r"|其他(?:路線|公車|車)(?:呢|嗎)?"
    r"|有(?:沒有|無)其他(?:路線|公車|車)"
)

# Rule 6: all-bus status at this stop ("還有車嗎", "末班車走了嗎", "現在還有哪些車").
# Must come after ARRIVAL_TIME so route-specific arrival queries are handled first.
_STOP_STATUS_RE = re.compile(
    r"末班車"
    r"|還有(?:幾路|哪些車|幾台|幾班|車(?:嗎|沒有)?)"
    r"|幾路(?:在|還在)跑"
    r"|現在還有"
    r"|現在幾點還有"
)

# Rule 5: routes serving this stop — needs "this stop" context clue.
_ROUTES_AT_STOP_RE = re.compile(
    r"(?:這站|這裡|這個站牌|本站|這邊)有(?:哪些|幾路?|什麼)(?:路線|公車|車)?"
    r"|在這裡?搭(?:哪|幾)"
)

# Rule 9: does route X stop at place Y?
_CHECK_STOP_CHARS = r"[^\s，。？！嗎呢]"
_CHECK_STOP_RE = re.compile(
    rf"(?<![A-Za-z\d])([A-Za-z]?\d{{2,4}}[A-Za-z]?)(?![A-Za-z\d])路?"
    rf"(?:有沒有停?|有停|停|有到|到)({_CHECK_STOP_CHARS}{{2,10}})(?:嗎|？|$)"
)

# Rule 8: list all stops of a route ("201停哪些站", "7120的站牌").
_ROUTE_STOPS_RE = re.compile(
    r"(?<![A-Za-z\d])([A-Za-z]?\d{2,4}[A-Za-z]?)(?![A-Za-z\d])路?"
    r"(?:停哪些站?|有哪些站|的站牌|站牌有哪些|停靠哪些站?|停哪裡)"
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

        # Rule 7: real-time arrival query — route explicit in text OR from last_route.
        # "幾點有車" after confirming 7120 should reuse state.last_route, not fall to LLM.
        route_match = _ROUTE_RE.search(text)
        if _ARRIVAL_QUESTION_RE.search(text):
            route = route_match.group(1) if route_match else state.last_route
            if route:
                return Decision(
                    intent=Intent.ARRIVAL_TIME,
                    tool_call=("get_arrivals_here", {"route": route}),
                    next_state=ConvState(
                        last_route=route,
                        last_destination=state.last_destination,
                        last_intent=Intent.ARRIVAL_TIME,
                    ),
                )

        # Rule 4 follow-up: "還有其他路線嗎" when last_destination is known.
        if _OTHER_ROUTES_RE.search(text) and state.last_destination:
            dest = state.last_destination
            return Decision(
                intent=Intent.OTHER_ROUTES_FOLLOWUP,
                tool_call=("find_routes_to_destination", {"destination": dest}),
                next_state=ConvState(
                    last_route=state.last_route,
                    last_destination=dest,
                    last_intent=Intent.OTHER_ROUTES_FOLLOWUP,
                ),
            )

        # Rule 4: find routes to a local destination.
        dest_match = _DEST_RE.search(text)
        if dest_match:
            dest = next(
                (g for g in dest_match.groups() if g is not None), ""
            ).strip()
            if dest:
                return Decision(
                    intent=Intent.FIND_ROUTES_TO_DEST,
                    tool_call=("find_routes_to_destination", {"destination": dest}),
                    next_state=ConvState(
                        last_route=state.last_route,
                        last_destination=dest,
                        last_intent=Intent.FIND_ROUTES_TO_DEST,
                    ),
                )

        # Rule 6: all-bus status at this stop (no specific route number).
        if _STOP_STATUS_RE.search(text):
            return Decision(
                intent=Intent.STOP_STATUS,
                tool_call=("get_stop_arrival_statuses_here", {}),
                next_state=ConvState(
                    last_route=state.last_route,
                    last_destination=state.last_destination,
                    last_intent=Intent.STOP_STATUS,
                ),
            )

        # Rule 5: list routes serving this stop.
        if _ROUTES_AT_STOP_RE.search(text):
            return Decision(
                intent=Intent.ROUTES_AT_STOP,
                tool_call=("get_routes_at_stop_here", {}),
                next_state=ConvState(
                    last_route=state.last_route,
                    last_destination=state.last_destination,
                    last_intent=Intent.ROUTES_AT_STOP,
                ),
            )

        # Rule 8: list all stops of a route — must run before Rule 9 because
        # "201停哪些站" would also match the CHECK_STOP_ON_ROUTE pattern (stop_name="哪些站").
        stops_match = _ROUTE_STOPS_RE.search(text)
        if stops_match:
            route = stops_match.group(1)
            return Decision(
                intent=Intent.ROUTE_STOPS_CLARIFY,
                tool_call=("get_route_stops", {"route": route}),
                next_state=ConvState(
                    last_route=route,
                    last_destination=state.last_destination,
                    last_intent=Intent.ROUTE_STOPS_CLARIFY,
                ),
            )

        # Rule 9: does a specific route stop at a named place?
        check_match = _CHECK_STOP_RE.search(text)
        if check_match:
            route = check_match.group(1)
            stop_name = check_match.group(2).strip()
            return Decision(
                intent=Intent.CHECK_STOP_ON_ROUTE,
                tool_call=("check_stop_on_route", {"route": route, "stop_name": stop_name}),
                next_state=ConvState(
                    last_route=route,
                    last_destination=state.last_destination,
                    last_intent=Intent.CHECK_STOP_ON_ROUTE,
                ),
            )

        # Everything else → legacy LLM loop.
        return Decision(intent=Intent.UNKNOWN, fallback_to_llm=True)
