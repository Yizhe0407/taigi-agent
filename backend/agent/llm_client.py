"""LLM chat-completion call with retry + context-window signalling.

`call_llm` is the only place that talks to the OpenAI-compatible client.
It absorbs transient errors with exponential backoff, marks telemetry, and
raises `ContextWindowExceeded` so `AgentSession` can compact + retry instead
of bubbling a 4xx to the caller.
"""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from typing import Any

from agent.diagnostics import log_diagnostic
from agent.error import summarize_error
from telemetry import AgentTelemetry

_LLM_MAX_RETRIES = 3

_CONTEXT_ERROR_MARKERS = (
    "context length",
    "context_length",
    "context window",
    "maximum context",
    "prompt too long",
    "prompt is too long",
    "too many tokens",
)

# Groq-specific strings. If switching to vLLM or another backend, update these
# markers — mismatches cause silent fall-through instead of mode retry.
_TOOL_CALL_FAILED_MARKERS = (
    "tool_use_failed",
    "failed to call a function",
)


class ContextWindowExceeded(RuntimeError):
    """LLM 拒收目前 messages，需縮減歷史後重試。"""


class ToolCallFailed(RuntimeError):
    """Model 生成了無效的 tool call 格式；session 應改用 auto 模式重試。"""


def _looks_like_context_error(error: Exception) -> bool:
    text = str(error).lower()
    return any(marker in text for marker in _CONTEXT_ERROR_MARKERS)


def _looks_like_tool_call_failure(error: Exception) -> bool:
    text = str(error).lower()
    return any(marker in text for marker in _TOOL_CALL_FAILED_MARKERS)


def _output_content(response: Any) -> str:
    """Serialize the completion message (content + tool calls) for span capture.

    Best-effort: returns "" on unexpected response shapes so capture can never
    break the success path (set_content skips empty strings).
    """
    try:
        message = response.choices[0].message
        payload: dict[str, Any] = {"content": message.content}
        tool_calls = [
            {"name": call.function.name, "arguments": call.function.arguments}
            for call in (message.tool_calls or [])
            if getattr(call, "type", "function") == "function"
        ]
        if tool_calls:
            payload["tool_calls"] = tool_calls
        return json.dumps(payload, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        return ""


def _assemble_stream_response(content_parts: list[str], tool_calls_acc: dict[int, dict]) -> Any:
    """Build a chat-completion-shaped response from accumulated stream deltas.

    Mirrors the non-streaming response shape the session loop reads:
    `.choices[0].message.content` / `.tool_calls[*].id/.type/.function.name/.arguments`.
    """
    tool_calls = [
        SimpleNamespace(
            id=acc["id"],
            type="function",
            function=SimpleNamespace(name=acc["name"], arguments="".join(acc["arguments"])),
        )
        for _, acc in sorted(tool_calls_acc.items())
    ]
    message = SimpleNamespace(content="".join(content_parts), tool_calls=tool_calls or None)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


async def call_llm_stream(
    client: Any,
    model: str,
    messages: list[dict],
    tools: list | None,
    extra_body: dict,
    telemetry: AgentTelemetry,
    *,
    operation: str,
    tool_choice: str = "auto",
):
    """`call_llm` 的串流版：yield ("delta", 文字增量) …("response", 組裝完整回應)。

    重試只在第一個 delta 之前——之後重試會讓已播出的內容重講一次。
    看到 tool_call delta 就停止對外 yield content（那是前導推理，不該進 TTS），
    但仍完整累積供歷史使用。
    """
    for attempt in range(_LLM_MAX_RETRIES):
        retry_error: Exception | None = None
        started = time.perf_counter()
        delta_emitted = False
        with telemetry.start_span(
            "agent.llm.call",
            {
                "agent.llm.model": model,
                "agent.llm.operation": operation,
                "agent.llm.attempt": attempt + 1,
                "agent.llm.stream": True,
            },
        ) as span:
            telemetry.set_content(
                span,
                "gen_ai.input.messages",
                json.dumps(messages, ensure_ascii=False),
                limit=8_000,
            )
            content_parts: list[str] = []
            tool_calls_acc: dict[int, dict] = {}
            try:
                stream = await client.chat.completions.create(
                    model=model,
                    messages=messages,
                    tools=tools,
                    tool_choice=tool_choice if tools else "none",
                    extra_body=extra_body,
                    stream=True,
                )
                async for chunk in stream:
                    if not getattr(chunk, "choices", None):
                        continue
                    delta = chunk.choices[0].delta
                    for call in delta.tool_calls or []:
                        acc = tool_calls_acc.setdefault(call.index, {"id": None, "name": None, "arguments": []})
                        if getattr(call, "id", None):
                            acc["id"] = call.id
                        function = getattr(call, "function", None)
                        if function is not None:
                            if getattr(function, "name", None):
                                acc["name"] = function.name
                            if getattr(function, "arguments", None):
                                acc["arguments"].append(function.arguments)
                    content = getattr(delta, "content", None)
                    if content:
                        content_parts.append(content)
                        if not tool_calls_acc:
                            delta_emitted = True
                            yield ("delta", content)
            except Exception as e:
                error_type = "context_window" if _looks_like_context_error(e) else type(e).__name__
                telemetry.record_llm_duration(
                    time.perf_counter() - started,
                    model=model,
                    operation=operation,
                    outcome="error",
                )
                telemetry.mark_span_error(span, error_type=error_type, exception=e)
                if _looks_like_context_error(e):
                    raise ContextWindowExceeded(str(e)) from e
                if _looks_like_tool_call_failure(e):
                    raise ToolCallFailed(str(e)) from e
                if delta_emitted or attempt == _LLM_MAX_RETRIES - 1:
                    raise
                telemetry.record_llm_retry(operation=operation, error_type=error_type)
                retry_error = e
            else:
                response = _assemble_stream_response(content_parts, tool_calls_acc)
                telemetry.record_llm_duration(
                    time.perf_counter() - started,
                    model=model,
                    operation=operation,
                    outcome="ok",
                )
                telemetry.set_content(span, "gen_ai.output.messages", _output_content(response))
                yield ("response", response)
                return

        if retry_error is not None:
            wait = 2**attempt
            log_diagnostic(
                "retry",
                f"LLM 串流呼叫失敗（{summarize_error(retry_error)}），{wait}s 後重試...",
            )
            await asyncio.sleep(wait)

    raise RuntimeError("LLM stream retry loop ended unexpectedly")


async def call_llm(
    client: Any,
    model: str,
    messages: list[dict],
    tools: list | None,
    extra_body: dict,
    telemetry: AgentTelemetry,
    *,
    operation: str,
    tool_choice: str = "required",
):
    """呼叫 LLM，暫時性錯誤退避重試，context overflow 交回 session 修復。"""
    for attempt in range(_LLM_MAX_RETRIES):
        retry_error: Exception | None = None
        started = time.perf_counter()
        with telemetry.start_span(
            "agent.llm.call",
            {
                "agent.llm.model": model,
                "agent.llm.operation": operation,
                "agent.llm.attempt": attempt + 1,
            },
        ) as span:
            telemetry.set_content(
                span,
                "gen_ai.input.messages",
                json.dumps(messages, ensure_ascii=False),
                limit=8_000,
            )
            try:
                response = await client.chat.completions.create(
                    model=model,
                    messages=messages,
                    tools=tools,
                    tool_choice=tool_choice if tools else "none",
                    extra_body=extra_body,
                )
            except Exception as e:
                error_type = "context_window" if _looks_like_context_error(e) else type(e).__name__
                telemetry.record_llm_duration(
                    time.perf_counter() - started,
                    model=model,
                    operation=operation,
                    outcome="error",
                )
                telemetry.mark_span_error(
                    span,
                    error_type=error_type,
                    exception=e,
                )
                if _looks_like_context_error(e):
                    raise ContextWindowExceeded(str(e)) from e
                if _looks_like_tool_call_failure(e):
                    raise ToolCallFailed(str(e)) from e
                if attempt == _LLM_MAX_RETRIES - 1:
                    raise
                telemetry.record_llm_retry(
                    operation=operation,
                    error_type=error_type,
                )
                retry_error = e
            else:
                telemetry.record_llm_duration(
                    time.perf_counter() - started,
                    model=model,
                    operation=operation,
                    outcome="ok",
                )
                telemetry.set_content(span, "gen_ai.output.messages", _output_content(response))
                return response

        if retry_error is not None:
            wait = 2**attempt
            log_diagnostic(
                "retry",
                f"LLM 呼叫失敗（{summarize_error(retry_error)}），{wait}s 後重試...",
            )
            await asyncio.sleep(wait)

    raise RuntimeError("LLM retry loop ended unexpectedly")
