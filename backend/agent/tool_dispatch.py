"""Tool-call execution helpers used by `AgentSession`.

Converts an LLM `tool_calls` array into role=tool messages, reporting per-call
outcome to telemetry. Each tool result must reference the matching
`tool_call_id` — otherwise the next LLM round fails on an unpaired result.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from telemetry import AgentTelemetry

ToolHandler = Callable[..., Awaitable[str]]


class _ToolCallFunction(Protocol):
    name: str
    arguments: str


class ToolCall(Protocol):
    """Shape this module relies on: an OpenAI SDK tool-call object, or a test double

    (e.g. `SimpleNamespace`/`_assemble_stream_response`'s stand-ins) with the same
    attributes. Kept as a `Protocol` instead of importing the SDK type because the
    streamed-response path assembles plain `SimpleNamespace` objects with this
    shape rather than real SDK instances.
    """

    id: str
    type: str
    function: _ToolCallFunction


@dataclass(frozen=True)
class ToolCallResult:
    """One tool call's outcome: the LLM-facing message plus a structured error flag.

    `message` is appended to `AgentSession.messages` verbatim — its `content`
    string keeps the "工具 X 執行失敗：" / "錯誤：" prefixes that the system
    prompt's 聽錯救援 logic depends on. `is_error` is for internal control flow
    only (e.g. `_find_direct_response`) so callers don't need to re-parse that
    prefix.
    """

    message: dict[str, str]
    is_error: bool


def function_tool_calls(message: Any) -> list[ToolCall]:
    return [call for call in message.tool_calls or [] if call.type == "function"]


def assistant_message(message: Any, tool_calls: list[ToolCall]) -> dict:
    if not tool_calls:
        return {"role": "assistant", "content": message.content}

    return {
        "role": "assistant",
        "content": message.content,
        "tool_calls": [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.function.name,
                    "arguments": call.function.arguments,
                },
            }
            for call in tool_calls
        ],
    }


def tool_result_msg(call_id: str, message: str) -> dict:
    return {"role": "tool", "tool_call_id": call_id, "content": message}


async def _execute_one(
    call: ToolCall,
    handlers: Mapping[str, ToolHandler],
    telemetry: AgentTelemetry,
) -> ToolCallResult:
    tool_name = call.function.name
    started = time.perf_counter()
    outcome = "ok"
    with telemetry.start_span(
        "agent.tool.call",
        {"agent.tool.name": tool_name, "agent.tool.call_id": call.id},
    ) as span:
        # Outside the try/finally on purpose: AgentTelemetry.set_content is
        # documented to never raise (best-effort, catches internally), so
        # wrapping it would only obscure that it can't affect `outcome`/finally.
        telemetry.set_content(span, "agent.tool.arguments", call.function.arguments)
        try:
            try:
                tool_args = json.loads(call.function.arguments or "{}")
            except (TypeError, json.JSONDecodeError) as e:
                outcome = "invalid_arguments"
                telemetry.record_tool_error(tool_name=tool_name, error_type=outcome)
                telemetry.mark_span_error(span, error_type=outcome, exception=e)
                msg = tool_result_msg(call.id, f"錯誤：工具參數格式有誤，無法執行 {tool_name}")
                return ToolCallResult(msg, is_error=True)

            if not isinstance(tool_args, dict):
                outcome = "invalid_arguments"
                telemetry.record_tool_error(tool_name=tool_name, error_type=outcome)
                telemetry.mark_span_error(span, error_type=outcome)
                msg = tool_result_msg(call.id, f"錯誤：工具參數必須是 JSON object，無法執行 {tool_name}")
                return ToolCallResult(msg, is_error=True)

            handler = handlers.get(tool_name)
            if handler is None:
                outcome = "missing_handler"
                telemetry.record_tool_error(tool_name=tool_name, error_type=outcome)
                telemetry.mark_span_error(span, error_type=outcome)
                msg = tool_result_msg(call.id, f"錯誤：找不到工具 {tool_name}")
                return ToolCallResult(msg, is_error=True)

            try:
                result = await handler(**tool_args)
            except Exception as e:
                outcome = "handler_error"
                telemetry.record_tool_error(tool_name=tool_name, error_type=type(e).__name__)
                telemetry.mark_span_error(span, error_type=type(e).__name__, exception=e)
                result = f"工具 {tool_name} 執行失敗：{e}"

            # tool_call_id 必須對應 assistant message 裡同一個 call id。
            telemetry.set_content(span, "agent.tool.result", str(result))
            msg = tool_result_msg(call.id, str(result))
            return ToolCallResult(msg, is_error=(outcome != "ok"))
        finally:
            telemetry.record_tool_duration(
                time.perf_counter() - started,
                tool_name=tool_name,
                outcome=outcome,
            )


async def execute_tool_calls(
    tool_calls: list[ToolCall],
    handlers: Mapping[str, ToolHandler],
    telemetry: AgentTelemetry,
) -> list[ToolCallResult]:
    # No return_exceptions=True: `_execute_one` already catches every exception
    # a handler/arg-parsing step can raise and turns it into an error
    # ToolCallResult, so nothing from tool-call handling should escape gather.
    # If something still does, it's an unanticipated bug (e.g. a malformed
    # `call` object) that should fail the turn loudly rather than be silently
    # swallowed into a partial result list with mismatched tool_call_ids.
    return list(await asyncio.gather(*[_execute_one(call, handlers, telemetry) for call in tool_calls]))
