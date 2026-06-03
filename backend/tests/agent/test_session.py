import asyncio
from contextlib import contextmanager
from types import SimpleNamespace

from agent.context import ContextStore
from agent.error import summarize_error
from agent.session import AgentSession


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

    assert asyncio.run(session.respond("還有車嗎")) == "查詢逾時，請換個方式再問一次。"
    assert session.messages == [
        {"role": "user", "content": "還有車嗎"},
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

    # Use a query that still reaches the LLM loop after Cut 2.2 migration.
    # "201幾點到" is now handled by the router (ARRIVAL_TIME tool path).
    # "還有車嗎" falls through to LLM (STOP_STATUS not yet migrated).
    assert asyncio.run(session.respond("還有車嗎")) == "有車還在跑"
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


def test_session_retries_when_tool_call_violates_injected_rule():
    async def enricher(_):
        return "\n[規則1：使用者只輸入路線號碼，禁止呼叫任何工具]"

    executed = []

    async def fake_tool():
        executed.append(True)
        return "不該被執行"

    session = make_session(
        [
            llm_response(assistant_message(tool_calls=[tool_call("bus", "{}", "t1")])),
            llm_response(assistant_message("請問您想查什麼資訊？")),
        ],
        input_enricher=enricher,
        tool_handlers={"bus": fake_tool},
    )

    # "還有車嗎" falls through to the LLM path (STOP_STATUS not yet migrated),
    # exercising the enricher → forbidden-tool retry under test.
    result = asyncio.run(session.respond("還有車嗎"))

    assert result == "請問您想查什麼資訊？"
    assert executed == []  # tool never ran
    tool_msgs = [m for m in session.messages if m["role"] == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0]["tool_call_id"] == "t1"
    assert "禁止呼叫" in tool_msgs[0]["content"]


def test_session_rule_retry_falls_through_after_max_retries():
    async def enricher(_):
        return "\n禁止呼叫任何工具"

    executed = []

    async def fake_tool():
        executed.append(True)
        return "執行了"

    session = make_session(
        [
            llm_response(assistant_message(tool_calls=[tool_call("bus", "{}", "t1")])),
            llm_response(assistant_message(tool_calls=[tool_call("bus", "{}", "t2")])),
            llm_response(assistant_message(tool_calls=[tool_call("bus", "{}", "t3")])),
            llm_response(assistant_message("最終回答")),
        ],
        input_enricher=enricher,
        tool_handlers={"bus": fake_tool},
    )

    # "還有車嗎" falls through to the LLM path (STOP_STATUS not yet migrated),
    # exercising the enricher → forbidden-tool retry path under test.
    result = asyncio.run(session.respond("還有車嗎"))

    assert result == "最終回答"
    assert executed == [True]  # exactly one real tool execution after retries exhausted


def test_session_compacts_old_history_with_transcript_and_summary(tmp_path):
    session = make_session(
        [
            llm_response(assistant_message("- 舊問題摘要")),
            llm_response(assistant_message("新回答")),
        ],
        context_store=ContextStore(tmp_path),
        max_history_tokens=120,
    )
    session.messages = [
        {"role": "user", "content": "舊問題 " * 200},
        {"role": "assistant", "content": "舊回答"},
    ]

    assert asyncio.run(session.respond("新問題")) == "新回答"
    assert session.client.chat.completions.calls[0]["tools"] is None
    assert session.messages[0]["content"].startswith("[先前對話已壓縮]")
    transcripts = list((tmp_path / "transcripts").glob("*.jsonl"))
    assert len(transcripts) == 1
    assert "舊問題" in transcripts[0].read_text(encoding="utf-8")


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
        [llm_response(assistant_message("這站有201等路線"))]
    )
    # "這站有哪些路線" → ROUTES_AT_STOP (not yet migrated) → falls through to LLM.
    # "我想去虎尾科大" now goes through the tool_respond path (FIND_ROUTES_TO_DEST).
    reply = asyncio.run(session.respond("這站有哪些路線"))
    assert reply == "這站有201等路線"
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
