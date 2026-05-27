"""LLM chat-completion call with retry + context-window signalling.

`call_llm` is the only place that talks to the OpenAI-compatible client.
It absorbs transient errors with exponential backoff, marks telemetry, and
raises `ContextWindowExceeded` so `AgentSession` can compact + retry instead
of bubbling a 4xx to the caller.
"""

from __future__ import annotations

import time
from typing import Any

from agent.error import summarize_error
from agent.telemetry import AgentTelemetry

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


class ContextWindowExceeded(RuntimeError):
    """LLM 拒收目前 messages，需縮減歷史後重試。"""


def _looks_like_context_error(error: Exception) -> bool:
    text = str(error).lower()
    return any(marker in text for marker in _CONTEXT_ERROR_MARKERS)


def call_llm(
    client: Any,
    model: str,
    messages: list[dict],
    tools: list | None,
    extra_body: dict,
    telemetry: AgentTelemetry,
    *,
    operation: str,
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
            try:
                response = client.chat.completions.create(
                    model=model,
                    messages=messages,
                    tools=tools,
                    extra_body=extra_body,
                )
            except Exception as e:
                error_type = (
                    "context_window"
                    if _looks_like_context_error(e)
                    else type(e).__name__
                )
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
                return response

        if retry_error is not None:
            wait = 2 ** attempt
            print(
                f"[retry] LLM 呼叫失敗（{summarize_error(retry_error)}），"
                f"{wait}s 後重試..."
            )
            time.sleep(wait)

    raise RuntimeError("LLM retry loop ended unexpectedly")
