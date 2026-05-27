"""Tool-call execution helpers used by `AgentSession`.

Converts an LLM `tool_calls` array into role=tool messages, reporting per-call
outcome to telemetry. Each tool result must reference the matching
`tool_call_id` — otherwise the next LLM round fails on an unpaired result.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from typing import Any

from agent.telemetry import AgentTelemetry

ToolHandler = Callable[..., str]


def function_tool_calls(message: Any) -> list[Any]:
    return [call for call in message.tool_calls or [] if call.type == "function"]


def assistant_message(message: Any, tool_calls: list[Any]) -> dict:
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


def tool_error(call_id: str, message: str) -> dict:
    return {"role": "tool", "tool_call_id": call_id, "content": message}


def execute_tool_calls(
    tool_calls: list[Any],
    handlers: Mapping[str, ToolHandler],
    telemetry: AgentTelemetry,
) -> list[dict]:
    results = []
    for call in tool_calls:
        tool_name = call.function.name
        started = time.perf_counter()
        outcome = "ok"
        with telemetry.start_span(
            "agent.tool.call",
            {
                "agent.tool.name": tool_name,
                "agent.tool.call_id": call.id,
            },
        ) as span:
            try:
                try:
                    tool_args = json.loads(call.function.arguments or "{}")
                except (TypeError, json.JSONDecodeError) as e:
                    outcome = "invalid_arguments"
                    telemetry.record_tool_error(
                        tool_name=tool_name,
                        error_type=outcome,
                    )
                    telemetry.mark_span_error(
                        span,
                        error_type=outcome,
                        exception=e,
                    )
                    results.append(
                        tool_error(
                            call.id,
                            f"錯誤：工具參數格式有誤，無法執行 {tool_name}",
                        )
                    )
                    continue

                if not isinstance(tool_args, dict):
                    outcome = "invalid_arguments"
                    telemetry.record_tool_error(
                        tool_name=tool_name,
                        error_type=outcome,
                    )
                    telemetry.mark_span_error(span, error_type=outcome)
                    results.append(
                        tool_error(
                            call.id,
                            f"錯誤：工具參數必須是 JSON object，無法執行 {tool_name}",
                        )
                    )
                    continue

                handler = handlers.get(tool_name)
                if handler is None:
                    outcome = "missing_handler"
                    telemetry.record_tool_error(
                        tool_name=tool_name,
                        error_type=outcome,
                    )
                    telemetry.mark_span_error(span, error_type=outcome)
                    results.append(
                        tool_error(call.id, f"錯誤：找不到工具 {tool_name}")
                    )
                    continue

                try:
                    result = handler(**tool_args)
                except Exception as e:
                    outcome = "handler_error"
                    telemetry.record_tool_error(
                        tool_name=tool_name,
                        error_type=type(e).__name__,
                    )
                    telemetry.mark_span_error(
                        span,
                        error_type=type(e).__name__,
                        exception=e,
                    )
                    result = f"工具 {tool_name} 執行失敗：{e}"

                # tool_call_id 必須對應 assistant message 裡同一個 call id。
                results.append(tool_error(call.id, str(result)))
            finally:
                telemetry.record_tool_duration(
                    time.perf_counter() - started,
                    tool_name=tool_name,
                    outcome=outcome,
                )

    return results
