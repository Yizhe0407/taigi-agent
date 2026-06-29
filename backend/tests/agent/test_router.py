"""Unit tests for `agent.router.IntentRouter`.

Router now handles only canned-response intents (Rules 1-3); tool dispatch
is handled by the LLM. Tests cover Rules 1-3, UNKNOWN fallthrough, and
Decision/ConvState invariants.
"""

from __future__ import annotations

import pytest

from agent.router import (
    ConvState,
    Decision,
    Intent,
    IntentRouter,
)


@pytest.fixture
def router() -> IntentRouter:
    return IntentRouter()


@pytest.fixture
def empty_state() -> ConvState:
    return ConvState()


# ── Rule 1: ROUTE_ONLY ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "user_input,expected_route",
    [
        ("201", "201"),
        ("201路", "201"),
        ("7000b", "7000b"),
        ("Y01", "Y01"),
        ("7126", "7126"),
        ("201 ", "201"),  # trailing whitespace
        (" 201", "201"),  # leading whitespace
        ("２０１", "201"),  # full-width digits normalized
    ],
)
def test_route_only_fires_for_bare_route_numbers(router: IntentRouter, empty_state: ConvState, user_input: str, expected_route: str):
    decision = router.classify(user_input, empty_state)
    assert decision.intent == Intent.ROUTE_ONLY
    assert decision.canned_response is not None
    assert expected_route in decision.canned_response
    assert decision.next_state is not None
    assert decision.next_state.last_route == expected_route
    assert decision.next_state.last_intent == Intent.ROUTE_ONLY


@pytest.mark.parametrize(
    "user_input",
    [
        "201幾點到",  # has question
        "201呢",  # has particle
        "201停哪些站",  # has verb
        "搭201可以嗎",  # has verb
        "去201",  # not a question about the route
    ],
)
def test_route_only_does_not_fire_when_question_present(router: IntentRouter, empty_state: ConvState, user_input: str):
    decision = router.classify(user_input, empty_state)
    assert decision.intent != Intent.ROUTE_ONLY


def test_route_only_preserves_last_destination(router: IntentRouter):
    """Confirming a route doesn't wipe out the destination from prior turns."""
    state = ConvState(last_destination="虎尾科大")
    decision = router.classify("201", state)
    assert decision.next_state is not None
    assert decision.next_state.last_destination == "虎尾科大"


# ── Rule 2: REMOTE_DESTINATION ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "user_input",
    [
        "我要去台中怎麼搭",
        "到台北要怎麼轉乘",
        "要不要轉乘才能到嘉義",
        "台北的車呢",
        "怎麼到高雄",
        "去南投有公車嗎",
        "搭什麼到彰化",
        "想去新竹",
        "臺中的車",
    ],
)
def test_remote_destination_fires_for_cross_county_queries(router: IntentRouter, empty_state: ConvState, user_input: str):
    decision = router.classify(user_input, empty_state)
    assert decision.intent == Intent.REMOTE_DESTINATION
    assert decision.canned_response == "這段要用地圖規劃比較準喔。"


def test_remote_destination_transfer_keyword_alone_triggers(router: IntentRouter, empty_state: ConvState):
    """『轉乘』anywhere → remote, even without a remote-city name."""
    decision = router.classify("我要轉乘", empty_state)
    assert decision.intent == Intent.REMOTE_DESTINATION


def test_remote_destination_does_not_fire_for_local_destination(router: IntentRouter, empty_state: ConvState):
    decision = router.classify("我想去虎尾科大", empty_state)
    assert decision.intent != Intent.REMOTE_DESTINATION


# ── Rule 3: TIMETABLE_UNSUPPORTED ────────────────────────────────────────────


@pytest.mark.parametrize(
    "user_input",
    [
        "201路完整時刻表",
        "201的時刻表",
        "班次表",
        "幾點幾分發車",
        "從這到斗六大概要幾分鐘",
        "從這裡到斗六要幾分",
    ],
)
def test_timetable_fires_for_schedule_queries(router: IntentRouter, empty_state: ConvState, user_input: str):
    decision = router.classify(user_input, empty_state)
    assert decision.intent == Intent.TIMETABLE_UNSUPPORTED
    assert decision.canned_response == "時刻表查不了，只能查下一班到站時間喔。"


@pytest.mark.parametrize(
    "user_input",
    [
        "幾點有車",  # real-time arrival, not a timetable
        "201幾點到",  # real-time arrival
        "下一班幾點來",  # real-time arrival
        "幾點來",
        "201幾點有車",
    ],
)
def test_timetable_does_not_misfire_on_realtime_arrival_queries(router: IntentRouter, empty_state: ConvState, user_input: str):
    """The Rule 3 / Rule 7 boundary that bit us in prod — guard it here."""
    decision = router.classify(user_input, empty_state)
    assert decision.intent != Intent.TIMETABLE_UNSUPPORTED


# ── UNKNOWN → fallback_to_llm ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "user_input",
    [
        "今天天氣很好",
        "你好",
        "我想去虎尾科大",  # tool dispatch → LLM
        "201幾點到",  # tool dispatch → LLM
        "還有車嗎",  # tool dispatch → LLM
        "這站有哪些路線",  # tool dispatch → LLM
    ],
)
def test_non_canned_intents_fall_back_to_llm(router: IntentRouter, empty_state: ConvState, user_input: str):
    decision = router.classify(user_input, empty_state)
    assert decision.intent == Intent.UNKNOWN
    assert decision.fallback_to_llm is True
    assert decision.canned_response is None


def test_empty_input_falls_back(router: IntentRouter, empty_state: ConvState):
    decision = router.classify("", empty_state)
    assert decision.intent == Intent.UNKNOWN
    assert decision.fallback_to_llm is True


def test_whitespace_only_input_falls_back(router: IntentRouter, empty_state: ConvState):
    decision = router.classify("   \t\n  ", empty_state)
    assert decision.intent == Intent.UNKNOWN


# ── Decision invariants ──────────────────────────────────────────────────────


def test_canned_response_decisions_do_not_fall_back(router: IntentRouter, empty_state: ConvState):
    """A canned-response Decision never also flags fallback_to_llm."""
    for input_str in ["201", "我要去台中", "完整時刻表"]:
        decision = router.classify(input_str, empty_state)
        assert decision.canned_response is not None
        assert decision.fallback_to_llm is False


def test_decision_dataclass_is_frozen():
    """Decisions are immutable so callers can't accidentally mutate them."""
    decision = Decision(intent=Intent.UNKNOWN, fallback_to_llm=True)
    with pytest.raises(Exception):
        decision.canned_response = "mutation"  # type: ignore[misc]


def test_conv_state_dataclass_is_frozen():
    state = ConvState(last_route="201")
    with pytest.raises(Exception):
        state.last_route = "999"  # type: ignore[misc]

    # Confirm removed field is gone.
    assert not hasattr(state, "pending_stops_clarify_route")
