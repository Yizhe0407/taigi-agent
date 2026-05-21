from types import SimpleNamespace

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

    def create(self, **kwargs):
        self.calls.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FakeClient:
    def __init__(self, responses):
        self.chat = SimpleNamespace(completions=FakeCompletions(responses))


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

    assert session.respond("還有車嗎") == "查詢逾時，請換個方式再問一次。"
    assert session.messages == [
        {"role": "user", "content": "還有車嗎"},
        {"role": "assistant", "content": "查詢逾時，請換個方式再問一次。"},
    ]


def test_session_returns_tool_errors_to_model_before_final_answer():
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
        ]
    )

    assert session.respond("查一下") == "已處理"
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


def test_session_retries_context_overflow_after_history_trim():
    session = make_session(
        [
            RuntimeError("maximum context length exceeded"),
            llm_response(assistant_message("恢復成功")),
        ]
    )
    session.messages = [
        {"role": "user", "content": "舊問題 " * 3000},
        {"role": "assistant", "content": "舊回答"},
    ]

    assert session.respond("新問題") == "恢復成功"
    assert len(session.client.chat.completions.calls) == 2
