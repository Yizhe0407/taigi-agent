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
            llm_response(assistant_message("201 快到了")),
        ],
        telemetry=telemetry,
        tool_handlers={"bus": bus_handler},
    )

    assert asyncio.run(session.respond("201")) == "201 快到了"
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
