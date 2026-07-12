"""Per-session extra-tool injection in api/chat.py (_rehydrate_session /
respond_in_session_stream).

Covers the voice pipeline's end_conversation hook: the injected tool runs its
handler and feeds the result back to the LLM, without leaking into the global
tool set or the REST path.
"""

import asyncio
from types import SimpleNamespace

from agent.session import AgentSession
from api import chat as chat_module

# ---------------------------------------------------------------------------
# Minimal LLM fakes (mirror tests/agent/test_session.py, kept self-contained).
# The first round is a forced non-stream tool call; later rounds stream deltas.
# ---------------------------------------------------------------------------


def _assistant(content="", tool_calls=None):
    return SimpleNamespace(content=content, tool_calls=tool_calls)


def _llm_response(message):
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def _tool_call(name, arguments, call_id):
    return SimpleNamespace(
        id=call_id,
        type="function",
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def _delta_chunk(content=None, tool_call_deltas=None):
    return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=content, tool_calls=tool_call_deltas))])


async def _as_chunk_stream(response):
    message = response.choices[0].message
    for i in range(0, len(message.content or ""), 8):
        yield _delta_chunk(content=(message.content or "")[i : i + 8])
    if message.tool_calls:
        yield _delta_chunk(
            tool_call_deltas=[
                SimpleNamespace(
                    index=i,
                    id=c.id,
                    type=c.type,
                    function=SimpleNamespace(name=c.function.name, arguments=c.function.arguments),
                )
                for i, c in enumerate(message.tool_calls)
            ]
        )


class _FakeCompletions:
    def __init__(self, responses):
        self.responses = list(responses)

    async def create(self, **kwargs):
        response = self.responses.pop(0)
        if kwargs.get("stream"):
            return _as_chunk_stream(response)
        return response


class _FakeClient:
    def __init__(self, responses):
        self.chat = SimpleNamespace(completions=_FakeCompletions(responses))


def _end_conversation_schema():
    return {
        "type": "function",
        "function": {
            "name": "end_conversation",
            "description": "結束對話",
            "parameters": {"type": "object", "properties": {}},
        },
    }


def _patch_make_session(monkeypatch, responses):
    """Make _rehydrate_session build a FakeClient-backed session, no LLM env."""

    def fake_make(_settings):
        return AgentSession(
            client=_FakeClient(responses),
            model="test-model",
            system_prompt="system",
            tool_schemas=[],
            tool_handlers={},
        )

    monkeypatch.setattr(chat_module, "make_agent_session", fake_make)
    monkeypatch.setattr(chat_module, "get_settings", lambda: None)


# ---------------------------------------------------------------------------
# (a) injected tool executes and its result is fed back to the LLM
# ---------------------------------------------------------------------------


def test_injected_extra_tool_runs_handler_and_feeds_result_back(monkeypatch):
    executed = []

    async def end_handler():
        executed.append(True)
        return "已標記結束"

    # Round 1 (forced): model calls the injected tool.
    # Round 2 (auto stream): model speaks the farewell after seeing the result.
    _patch_make_session(
        monkeypatch,
        [
            _llm_response(_assistant(tool_calls=[_tool_call("end_conversation", "{}", "c1")])),
            _llm_response(_assistant("再會啦。")),
        ],
    )

    session = chat_module._rehydrate_session(
        [],
        extra_tools=[(_end_conversation_schema(), end_handler)],
        extra_system_prompt="\n【結束對話】指示",
    )

    # "你好" is off-topic → LLM path (not a canned router intent).
    reply = asyncio.run(session.respond("你好"))

    assert executed == [True]
    assert reply == "再會啦。"
    # The handler's return value went back to the model as a tool result.
    assert any(m["role"] == "tool" and m["content"] == "已標記結束" for m in session.messages)
    # Prompt guidance was appended.
    assert session.system_prompt.endswith("【結束對話】指示")


def test_rehydrate_injection_does_not_mutate_globals(monkeypatch):
    """Injected tools must not leak into the base tool set of the next session."""
    _patch_make_session(monkeypatch, [])
    base = chat_module._rehydrate_session([])
    base_schema_ids = id(base.tool_schemas)
    base_handler_ids = id(base.tool_handlers)

    injected = chat_module._rehydrate_session([], extra_tools=[(_end_conversation_schema(), lambda: None)])

    assert "end_conversation" in injected.tool_handlers
    assert "end_conversation" not in base.tool_handlers
    # New session got fresh containers, not the base ones mutated in place.
    assert id(injected.tool_schemas) != base_schema_ids
    assert id(injected.tool_handlers) != base_handler_ids


# ---------------------------------------------------------------------------
# (b) REST path injects nothing
# ---------------------------------------------------------------------------


def test_rest_path_rehydrate_has_no_end_conversation(monkeypatch):
    _patch_make_session(monkeypatch, [])
    session = chat_module._rehydrate_session([])  # REST default: no extra tools

    names = [s["function"]["name"] for s in session.tool_schemas]
    assert "end_conversation" not in names
    assert "end_conversation" not in session.tool_handlers
    assert session.system_prompt == "system"  # unmodified
