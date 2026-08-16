import asyncio
from contextlib import contextmanager
from types import SimpleNamespace

from agent.error import summarize_error
from agent.router import Intent
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


def _delta_chunk(content=None, tool_call_deltas=None):
    return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=content, tool_calls=tool_call_deltas))])


async def _as_chunk_stream(response):
    """Replay a canned response as create(stream=True) delta chunks.

    Content arrives in small deltas (so sentence boundaries and <think> tags
    span chunks, like real token streams); tool calls arrive as one indexed
    delta batch.
    """
    message = response.choices[0].message
    content = message.content or ""
    for i in range(0, len(content), 8):
        yield _delta_chunk(content=content[i : i + 8])
    if message.tool_calls:
        yield _delta_chunk(
            tool_call_deltas=[
                SimpleNamespace(
                    index=i,
                    id=call.id,
                    type=call.type,
                    function=SimpleNamespace(name=call.function.name, arguments=call.function.arguments),
                )
                for i, call in enumerate(message.tool_calls)
            ]
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
        if kwargs.get("stream"):
            return _as_chunk_stream(response)
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
        self.turns = []
        self.contents = []

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

    def record_turn(self, *, path, intent, outcome, tool_rounds=None):
        self.turns.append((path, intent, outcome, tool_rounds))

    def set_content(self, span, key, text, *, limit=None):
        self.contents.append((getattr(span, "name", None), key, text))


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
            llm_response(assistant_message(tool_calls=[tool_call("bus", '{"route": "201"}', "bus-1")])),
            llm_response(assistant_message("有車還在跑")),
        ],
        telemetry=telemetry,
        tool_handlers={"bus": bus_handler},
    )

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
    assert [(item[1], item[2]) for item in telemetry.tool_durations] == [("bus", "ok")]
    assert telemetry.turns == [("llm", "unknown", "ok", 1)]


def test_session_records_turn_outcomes_for_canned_limit_and_error_paths():
    # Canned path: pure route number resolves without an LLM call.
    canned = RecordingTelemetry()
    session = make_session([], telemetry=canned)
    asyncio.run(session.respond("201"))
    assert canned.turns == [("canned", "route_only", "ok", None)]

    # Tool-round limit path.
    limited = RecordingTelemetry()
    session = make_session(
        [llm_response(assistant_message(tool_calls=[tool_call("bus", "{}", "c1")]))],
        max_tool_rounds=0,
        telemetry=limited,
    )
    asyncio.run(session.respond("你好"))
    assert limited.turns == [("llm", "unknown", "tool_round_limit", 0)]

    # Error path: context overflow persists past recovery → outcome=error.
    # (Uses context errors because they propagate without retry backoff sleeps.)
    failing = RecordingTelemetry()
    session = make_session(
        [
            RuntimeError("maximum context length exceeded"),
            RuntimeError("maximum context length exceeded"),
        ],
        telemetry=failing,
    )
    try:
        asyncio.run(session.respond("查一下"))
    except RuntimeError:
        pass
    assert failing.turns == [("llm", "unknown", "error", 0)]


def test_error_path_calls_trim_before_reraising():
    """The except branch in `_run_llm_loop` must trim history, not just the
    other exit paths (canned / normal-finish / tool_round_limit / respond_directly).

    Spies on the bound `_trim` method (not `_prepare_context`'s internal
    `trim_history` call, which runs earlier and unconditionally) so this pins
    down specifically that the error path's own `_trim()` call fires.
    """
    session = make_session([RuntimeError("boom")])
    trim_calls = []
    original_trim = session._trim

    def spy_trim():
        trim_calls.append(len(session.messages))
        original_trim()

    session._trim = spy_trim

    raised = None
    try:
        asyncio.run(session.respond("查一下"))
    except RuntimeError as e:
        raised = e

    assert raised is not None
    assert trim_calls == [1]  # user turn only — error fires before any tool round


def test_error_path_drops_dangling_tool_call_without_orphaning_history():
    """If `execute_tool_calls` itself raises (not a handler exception, which
    `_execute_one` already converts to an error ToolCallResult — see
    `agent/tool_dispatch.py`), `messages` is left with an assistant(tool_calls)
    entry and no matching `role: tool` results. The error path must drop that
    whole incomplete round instead of trimming it into permanent history as
    an orphaned tool_call_id.
    """
    import agent.session as session_module

    session = make_session(
        [llm_response(assistant_message(tool_calls=[tool_call("bus", "{}", "c1")]))],
    )

    async def failing_execute_tool_calls(tool_calls, handlers, telemetry):
        raise RuntimeError("dispatch exploded")

    original = session_module.execute_tool_calls
    session_module.execute_tool_calls = failing_execute_tool_calls
    try:
        raised = None
        try:
            asyncio.run(session.respond("到站時間"))
        except RuntimeError as e:
            raised = e
        assert raised is not None and str(raised) == "dispatch exploded"
    finally:
        session_module.execute_tool_calls = original

    # No assistant tool_calls entry may survive without every referenced
    # tool_call_id answered later in history.
    for i, msg in enumerate(session.messages):
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            ids = {call["id"] for call in msg["tool_calls"]}
            answered = {m["tool_call_id"] for m in session.messages[i + 1 :] if m.get("role") == "tool"}
            assert ids <= answered, "orphaned tool_call_id left in trimmed history"
    # Concretely for this scenario: the failed round is dropped entirely,
    # leaving only the user turn that started it.
    assert session.messages == [{"role": "user", "content": "到站時間"}]


def test_summarize_error_collapses_cloudflare_tunnel_html():
    error = RuntimeError("<!DOCTYPE html><head><title>Cloudflare Tunnel error | example</title></head><body>Error 1033 Cloudflare Tunnel error</body>")

    assert summarize_error(error) == ("Cloudflare Tunnel error 1033，LLM endpoint 目前無法連線")


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
    assert session.conv_state.last_intent == Intent.ROUTE_ONLY


def test_router_canned_response_for_remote_destination():
    session = make_session([])
    reply = asyncio.run(session.respond("我要去台中怎麼搭"))
    assert reply == "目前只能規劃雲林縣內行程，跨縣市請改用其他地圖服務查詢。"
    assert session.client.chat.completions.calls == []


def test_router_canned_response_for_timetable_query():
    session = make_session([])
    reply = asyncio.run(session.respond("201路完整時刻表"))
    assert reply == "時刻表查不了，只能查下一班到站時間喔。"
    assert session.client.chat.completions.calls == []


def test_respond_directly_discarded_when_called_with_other_tools():
    """respond_directly guessed content must lose to a real tool call in the same batch.

    Regression test for the bug where `tool_choice="required"` + parallel tool
    calls let the model pair a real lookup (bus) with a respond_directly guess
    generated before seeing any tool result — and the guess used to win.
    """

    async def bus_handler():
        return "查詢結果：還要五分鐘"

    async def respond_directly_handler(message):
        return message

    session = make_session(
        [
            llm_response(
                assistant_message(
                    tool_calls=[
                        tool_call("bus", "{}", "c1"),
                        tool_call("respond_directly", '{"message": "大約五分鐘後到"}', "c2"),
                    ]
                )
            ),
            llm_response(assistant_message("還要五分鐘")),
        ],
        tool_handlers={"bus": bus_handler, "respond_directly": respond_directly_handler},
    )

    reply = asyncio.run(session.respond("到站時間"))

    # The real tool result was fed back for a follow-up LLM call, not short-circuited.
    assert reply == "還要五分鐘"
    assert len(session.client.chat.completions.calls) == 2


def test_respond_directly_handler_error_falls_through_to_llm_round():
    """A respond_directly handler exception must not short-circuit as the final reply.

    `_find_direct_response` decides this from `ToolCallResult.is_error`
    (agent/tool_dispatch.py) — a sole-call respond_directly failure must still
    fall through to a follow-up LLM round.
    """

    async def failing_respond_directly(message, intent=None):
        raise RuntimeError("boom")

    session = make_session(
        [
            llm_response(assistant_message(tool_calls=[tool_call("respond_directly", '{"message": "guess"}', "c1")])),
            llm_response(assistant_message("修正後的回答")),
        ],
        tool_handlers={"respond_directly": failing_respond_directly},
    )

    reply = asyncio.run(session.respond("到站時間"))

    assert reply == "修正後的回答"
    assert len(session.client.chat.completions.calls) == 2
    tool_messages = [msg for msg in session.messages if msg["role"] == "tool"]
    assert tool_messages == [
        {"role": "tool", "tool_call_id": "c1", "content": "工具 respond_directly 執行失敗：boom"},
    ]


def test_respond_directly_short_circuit_stores_normalized_assistant_history():
    """respond_directly-only short-circuit must record the reply in history.

    `_finish_normalized` must append to `self.messages`, not just return the
    text — otherwise history ends on a `role: tool` message with no assistant
    turn, violating "assistant content == what was actually said" and
    corrupting every subsequent LLM call's context.
    """

    async def respond_directly_handler(message):
        return message

    session = make_session(
        [
            llm_response(assistant_message(tool_calls=[tool_call("respond_directly", '{"message": "<think>猜</think>公車還有五分鐘"}', "c1")])),
        ],
        tool_handlers={"respond_directly": respond_directly_handler},
    )

    reply = asyncio.run(session.respond("到站時間"))

    assert reply == "公車還有五分鐘"
    assert session.messages[-1] == {"role": "assistant", "content": "公車還有五分鐘"}
    # Only one LLM call — the short-circuit must not trigger a follow-up round.
    assert len(session.client.chat.completions.calls) == 1


def test_history_stores_normalized_assistant_content_not_raw_reasoning():
    """Assistant content saved to history must be think-stripped and traditional Chinese.

    Raw content (e.g. Qwen3's <think> blocks, simplified Chinese) left in
    history would waste context budget and few-shot the model into repeating
    its own dirty output.
    """
    session = make_session([llm_response(assistant_message("<think>我在想</think>没问题"))])

    reply = asyncio.run(session.respond("你好"))

    assert "<think>" not in reply
    assert reply == session.messages[-1]["content"]
    assert "<think>" not in session.messages[-1]["content"]
    assert "没" not in session.messages[-1]["content"]


def test_respond_stream_yields_sentences_and_history_matches_spoken_text():
    """串流不變式：respond 的完整回覆 == respond_stream 所有 chunk 串接，
    且歷史存的 assistant content 等於實際播出的文字（think 已剝、簡轉繁）。"""

    async def bus_handler():
        return "201 還有五分鐘"

    session = make_session(
        [
            llm_response(assistant_message(tool_calls=[tool_call("bus", "{}", "c1")])),
            llm_response(assistant_message("<think>检查结果</think>公车还有五分钟。请在原地等候。")),
        ],
        tool_handlers={"bus": bus_handler},
    )

    async def collect():
        return [chunk async for chunk in session.respond_stream("到站時間")]

    chunks = asyncio.run(collect())

    assert len(chunks) >= 2, "第二輪自由文字應逐句串流，而非整段一次"
    reply = "".join(chunks)
    assert reply == "公車還有五分鐘。請在原地等候。"
    assert session.messages[-1]["content"] == reply
    # 第二輪走 stream=True
    assert session.client.chat.completions.calls[1].get("stream") is True


def test_respond_stream_canned_path_yields_single_chunk():
    session = make_session([])

    async def collect():
        return [chunk async for chunk in session.respond_stream("我要去台中怎麼搭")]

    assert asyncio.run(collect()) == ["目前只能規劃雲林縣內行程，跨縣市請改用其他地圖服務查詢。"]


def test_router_fallthrough_still_calls_llm():
    """Non-router intents still reach the legacy LLM loop."""
    session = make_session([llm_response(assistant_message("你好，有需要查公車嗎？"))])
    # Off-topic input → UNKNOWN → falls through to LLM.
    reply = asyncio.run(session.respond("你好"))
    assert reply == "你好，有需要查公車嗎？"
    assert len(session.client.chat.completions.calls) == 1
