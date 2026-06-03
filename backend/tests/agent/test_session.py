import asyncio
from contextlib import contextmanager
from types import SimpleNamespace

from agent.error import summarize_error
from agent.router import Intent
from agent.session import AgentSession, _phrase_tool_result


def assistant_message(content="", tool_calls=None):
    return SimpleNamespace(content=content, tool_calls=tool_calls)


def llm_response(message):
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def tool_call(name, arguments, call_id):
    return SimpleNamespace(
        id=call_id,
        type="function",
        function=SimpleNamespace(name=name, arguments=arguments),
    )


class FakeCompletions:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FakeClient:
    def __init__(self, responses):
        self.chat = SimpleNamespace(completions=FakeCompletions(responses))


class RecordingSpan:
    def __init__(self, name, attributes):
        self.name = name
        self.attributes = attributes
        self.errors = []


class RecordingTelemetry:
    def __init__(self):
        self.spans = []
        self.llm_durations = []
        self.llm_retries = []
        self.tool_routes = []
        self.tool_durations = []
        self.tool_errors = []

    @contextmanager
    def start_span(self, name, attributes=None):
        span = RecordingSpan(name, attributes or {})
        self.spans.append(span)
        yield span

    def mark_span_error(
        self,
        span,
        *,
        error_type,
        exception=None,
        description=None,
    ):
        exception_type = type(exception).__name__ if exception else None
        span.errors.append((error_type, exception_type))

    def record_llm_duration(self, duration_s, *, model, operation, outcome):
        self.llm_durations.append((duration_s, model, operation, outcome))

    def record_llm_retry(self, *, operation, error_type):
        self.llm_retries.append((operation, error_type))

    def trace_tool_routing(self, tool_names, *, accepted):
        self.tool_routes.append((tuple(tool_names), accepted))

    def record_tool_duration(self, duration_s, *, tool_name, outcome):
        self.tool_durations.append((duration_s, tool_name, outcome))

    def record_tool_error(self, *, tool_name, error_type):
        self.tool_errors.append((tool_name, error_type))


def make_session(responses, **kwargs):
    return AgentSession(
        client=FakeClient(responses),
        model="test-model",
        system_prompt="system",
        tool_schemas=[],
        tool_handlers=kwargs.pop("tool_handlers", {}),
        **kwargs,
    )


def test_session_tool_limit_does_not_leave_unpaired_tool_call():
    session = make_session(
        [llm_response(assistant_message(tool_calls=[tool_call("bus", "{}", "c1")]))],
        max_tool_rounds=0,
    )

    # "你好" is off-topic → UNKNOWN → LLM path, exercises the tool-round limit.
    assert asyncio.run(session.respond("你好")) == "查詢逾時，請換個方式再問一次。"
    assert session.messages == [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "查詢逾時，請換個方式再問一次。"},
    ]


def test_session_returns_tool_errors_to_model_before_final_answer():
    telemetry = RecordingTelemetry()
    session = make_session(
        [
            llm_response(
                assistant_message(
                    tool_calls=[
                        tool_call("bad_args", "{", "bad"),
                        tool_call("missing", "{}", "missing"),
                    ]
                )
            ),
            llm_response(assistant_message("已處理")),
        ],
        telemetry=telemetry,
    )

    assert asyncio.run(session.respond("查一下")) == "已處理"
    tool_messages = [msg for msg in session.messages if msg["role"] == "tool"]
    assert tool_messages == [
        {
            "role": "tool",
            "tool_call_id": "bad",
            "content": "錯誤：工具參數格式有誤，無法執行 bad_args",
        },
        {
            "role": "tool",
            "tool_call_id": "missing",
            "content": "錯誤：找不到工具 missing",
        },
    ]
    assert telemetry.tool_errors == [
        ("bad_args", "invalid_arguments"),
        ("missing", "missing_handler"),
    ]


def test_session_retries_context_overflow_after_context_recovery():
    telemetry = RecordingTelemetry()
    session = make_session(
        [
            RuntimeError("maximum context length exceeded"),
            llm_response(assistant_message("恢復成功")),
        ],
        telemetry=telemetry,
    )

    assert asyncio.run(session.respond("新問題")) == "恢復成功"
    assert len(session.client.chat.completions.calls) == 2
    assert telemetry.llm_retries == [("respond", "context_window")]


def test_session_records_llm_tool_latency_and_routing():
    telemetry = RecordingTelemetry()
    async def bus_handler(route):
        return f"{route} 約 3 分鐘"

    session = make_session(
        [
            llm_response(
                assistant_message(
                    tool_calls=[tool_call("bus", '{"route": "201"}', "bus-1")]
                )
            ),
            llm_response(assistant_message("有車還在跑")),
        ],
        telemetry=telemetry,
        tool_handlers={"bus": bus_handler},
    )

    # Use a query that still reaches the LLM loop after Cut 2.3 migration.
    # Off-topic inputs like "你好" fall through to UNKNOWN → LLM path.
    assert asyncio.run(session.respond("你好")) == "有車還在跑"
    assert [span.name for span in telemetry.spans] == [
        "agent.turn",
        "agent.llm.call",
        "agent.tool.call",
        "agent.llm.call",
    ]
    assert telemetry.tool_routes == [(("bus",), True)]
    assert [item[2] for item in telemetry.llm_durations] == ["respond", "respond"]
    assert [item[3] for item in telemetry.llm_durations] == ["ok", "ok"]
    assert [(item[1], item[2]) for item in telemetry.tool_durations] == [
        ("bus", "ok")
    ]


def test_summarize_error_collapses_cloudflare_tunnel_html():
    error = RuntimeError(
        "<!DOCTYPE html><head><title>Cloudflare Tunnel error | example</title></head>"
        "<body>Error 1033 Cloudflare Tunnel error</body>"
    )

    assert summarize_error(error) == (
        "Cloudflare Tunnel error 1033，LLM endpoint 目前無法連線"
    )



# ── Router integration ───────────────────────────────────────────────────────


def test_router_canned_response_skips_llm_call():
    """ROUTE_ONLY input: router returns canned, LLM is never invoked."""
    session = make_session(
        # Empty response list — if the LLM is called, FakeCompletions raises
        # IndexError, which would fail this test loudly.
        [],
    )
    reply = asyncio.run(session.respond("201"))
    assert "201" in reply
    assert "您想查什麼" in reply
    # No LLM call was made.
    assert session.client.chat.completions.calls == []
    # Session messages still get the user + assistant turn recorded.
    assert session.messages == [
        {"role": "user", "content": "201"},
        {"role": "assistant", "content": reply},
    ]
    # ConvState updated.
    assert session.conv_state.last_route == "201"


def test_router_canned_response_for_remote_destination():
    session = make_session([])
    reply = asyncio.run(session.respond("我要去台中怎麼搭"))
    assert reply == "這段要用地圖規劃比較準喔。"
    assert session.client.chat.completions.calls == []


def test_router_canned_response_for_timetable_query():
    session = make_session([])
    reply = asyncio.run(session.respond("201路完整時刻表"))
    assert reply == "時刻表查不了，要查到站時間嗎？"
    assert session.client.chat.completions.calls == []


def test_router_fallthrough_still_calls_llm():
    """Non-router intents still reach the legacy LLM loop."""
    session = make_session(
        [llm_response(assistant_message("你好，有需要查公車嗎？"))]
    )
    # Off-topic input → UNKNOWN → falls through to LLM.
    reply = asyncio.run(session.respond("你好"))
    assert reply == "你好，有需要查公車嗎？"
    assert len(session.client.chat.completions.calls) == 1


def test_session_tool_call_decision_calls_tool_and_records_turn():
    """Router-dispatched tool_call path: tool executes, turn is recorded."""
    telemetry = RecordingTelemetry()

    async def fake_find_routes(destination: str) -> str:
        return "7120"

    session = make_session(
        [],  # no LLM calls needed for this path
        telemetry=telemetry,
        tool_handlers={"find_routes_to_destination": fake_find_routes},
    )

    reply = asyncio.run(session.respond("我想去虎尾科大"))

    assert reply == "搭7120就可以到虎尾科大。"
    assert session.messages == [
        {"role": "user", "content": "我想去虎尾科大"},
        {"role": "assistant", "content": reply},
    ]
    assert session.conv_state.last_destination == "虎尾科大"
    assert [span.name for span in telemetry.spans] == [
        "agent.turn",
        "agent.tool.call",
    ]
    assert [(item[1], item[2]) for item in telemetry.tool_durations] == [
        ("find_routes_to_destination", "ok")
    ]
    # No LLM call.
    assert session.client.chat.completions.calls == []


def test_session_tool_call_arrival_time_returns_raw_result():
    async def fake_arrivals(route: str) -> str:
        return "往高鐵雲林站：約3分鐘"

    session = make_session(
        [],
        tool_handlers={"get_arrivals_here": fake_arrivals},
    )

    reply = asyncio.run(session.respond("201幾點到"))

    assert reply == "往高鐵雲林站：約3分鐘"
    assert session.conv_state.last_route == "201"
    assert session.client.chat.completions.calls == []


def test_session_other_routes_followup_uses_state_destination():
    calls = []

    async def fake_find_routes(destination: str) -> str:
        calls.append(destination)
        return "本站沒有直達虎尾科大的路線"

    session = make_session(
        [],
        tool_handlers={"find_routes_to_destination": fake_find_routes},
    )
    from agent.router import ConvState
    session.conv_state = ConvState(last_destination="虎尾科大")

    reply = asyncio.run(session.respond("還有其他路線嗎"))

    assert calls == ["虎尾科大"]
    assert reply == "本站沒有直達虎尾科大的路線"


# ── _phrase_tool_result unit tests ───────────────────────────────────────────


def test_phrase_find_routes_single_with_direction():
    """Single route+direction → natural split, no destination repetition."""
    result = _phrase_tool_result(
        Intent.FIND_ROUTES_TO_DEST,
        "7120 往高鐵雲林站",
        {"destination": "虎尾科大"},
    )
    assert result == "搭7120就可以，往高鐵雲林站那班。"


def test_phrase_find_routes_single_no_direction():
    """Single route, all directions serve dest → include destination in reply."""
    result = _phrase_tool_result(
        Intent.FIND_ROUTES_TO_DEST,
        "7120",
        {"destination": "虎尾科大"},
    )
    assert result == "搭7120就可以到虎尾科大。"


def test_phrase_find_routes_multiple():
    """Multiple routes → keep direction labels, prefix with destination."""
    result = _phrase_tool_result(
        Intent.FIND_ROUTES_TO_DEST,
        "7120 往高鐵雲林站、7001 往斗六",
        {"destination": "虎尾科大"},
    )
    assert result == "去虎尾科大可以搭7120 往高鐵雲林站、7001 往斗六。"


def test_phrase_find_routes_passthrough_on_error():
    for error_result in ("雲林公車查詢失敗：…", "找不到路線", "本站沒有直達虎尾科大的路線"):
        assert _phrase_tool_result(
            Intent.FIND_ROUTES_TO_DEST, error_result, {"destination": "虎尾科大"}
        ) == error_result


def test_phrase_other_routes_strips_direction():
    """OTHER_ROUTES_FOLLOWUP strips direction labels — already in context."""
    result = _phrase_tool_result(
        Intent.OTHER_ROUTES_FOLLOWUP,
        "7120 往高鐵雲林站",
        {"destination": "虎尾科大"},
    )
    assert result == "就只有7120，沒有其他路線了。"


def test_phrase_other_routes_multiple_strips_directions():
    result = _phrase_tool_result(
        Intent.OTHER_ROUTES_FOLLOWUP,
        "7120 往高鐵雲林站、7001 往斗六",
        {"destination": "虎尾科大"},
    )
    assert result == "就只有7120、7001，沒有其他路線了。"
