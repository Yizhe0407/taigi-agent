import json
import re
import time
from collections.abc import Callable, Mapping
from typing import Any

from agent.context import (
    LONG_TOOL_RESULT_CHARS,
    MAX_HISTORY_TOKENS,
    ContextStore,
    compact_long_tool_results,
    compacted_history_message,
    estimate_tokens,
    split_latest_exchange,
    trim_history,
)
from agent.telemetry import AgentTelemetry

InputEnricher = Callable[[str], str]
ToolHandler = Callable[..., str]

_LLM_MAX_RETRIES = 3
_MAX_CONTEXT_RECOVERY_RETRIES = 1
_DEFAULT_MAX_TOOL_ROUNDS = 8
_TOOL_ROUND_LIMIT_MESSAGE = "查詢逾時，請換個方式再問一次。"
_COMPACT_SUMMARY_SYSTEM_PROMPT = """你負責壓縮一段公車查詢助理的對話歷史。
請保留使用者目標、已知站牌與方向、已查到的路線或到站狀態、
重要工具錯誤、尚未回答的問題與所有限制。
不要補充 transcript 沒有的事實。用繁體中文，條列簡短。"""
_CONTEXT_ERROR_MARKERS = (
    "context length",
    "context_length",
    "context window",
    "maximum context",
    "prompt too long",
    "prompt is too long",
    "too many tokens",
)
_HTML_TITLE_RE = re.compile(r"<title>\s*(.*?)\s*</title>", re.IGNORECASE | re.DOTALL)
_CLOUDFLARE_ERROR_RE = re.compile(r"Cloudflare Tunnel error", re.IGNORECASE)


class ContextWindowExceeded(RuntimeError):
    """LLM 拒收目前 messages，需縮減歷史後重試。"""


def _looks_like_context_error(error: Exception) -> bool:
    text = str(error).lower()
    return any(marker in text for marker in _CONTEXT_ERROR_MARKERS)


def summarize_error(error: Exception) -> str:
    """Keep CLI errors readable when upstreams return verbose HTML bodies."""
    text = str(error).strip()
    if not text:
        return type(error).__name__

    if _CLOUDFLARE_ERROR_RE.search(text):
        return "Cloudflare Tunnel error 1033，LLM endpoint 目前無法連線"

    title = _HTML_TITLE_RE.search(text)
    if title is not None:
        return f"{type(error).__name__}: HTML error page: {title.group(1).strip()}"

    first_line = " ".join(text.splitlines()[:1]).strip()
    if len(first_line) > 240:
        first_line = first_line[:237] + "..."
    return f"{type(error).__name__}: {first_line}"


def _call_llm(
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


def _function_tool_calls(message: Any) -> list[Any]:
    return [call for call in message.tool_calls or [] if call.type == "function"]


def _assistant_message(message: Any, tool_calls: list[Any]) -> dict:
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


def _tool_error(call_id: str, message: str) -> dict:
    return {"role": "tool", "tool_call_id": call_id, "content": message}


def _execute_tool_calls(
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
                        _tool_error(
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
                        _tool_error(
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
                        _tool_error(call.id, f"錯誤：找不到工具 {tool_name}")
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
                results.append(_tool_error(call.id, str(result)))
            finally:
                telemetry.record_tool_duration(
                    time.perf_counter() - started,
                    tool_name=tool_name,
                    outcome=outcome,
                )

    return results


def _compact_summary_request(messages: list[dict], transcript_path: str) -> list[dict]:
    transcript = json.dumps(messages, ensure_ascii=False)
    return [
        {"role": "system", "content": _COMPACT_SUMMARY_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"完整 transcript 已保存於 {transcript_path}。\n"
                "請摘要以下即將移出 active context 的 messages：\n"
                f"{transcript}"
            ),
        },
    ]


class AgentSession:
    """一段可持續的 agent 對話，不綁 CLI、語音或其他 I/O。"""

    def __init__(
        self,
        *,
        client: Any,
        model: str,
        system_prompt: str,
        tool_schemas: list,
        tool_handlers: Mapping[str, ToolHandler],
        input_enricher: InputEnricher | None = None,
        extra_body: dict | None = None,
        max_tool_rounds: int = _DEFAULT_MAX_TOOL_ROUNDS,
        max_history_tokens: int = MAX_HISTORY_TOKENS,
        compact_tool_result_chars: int = LONG_TOOL_RESULT_CHARS,
        context_store: ContextStore | None = None,
        telemetry: AgentTelemetry | None = None,
    ) -> None:
        self.client = client
        self.model = model
        self.system_prompt = system_prompt
        self.tool_schemas = tool_schemas
        self.tool_handlers = tool_handlers
        self.input_enricher = input_enricher
        self.extra_body = extra_body or {
            "chat_template_kwargs": {"enable_thinking": False}
        }
        self.max_tool_rounds = max_tool_rounds
        self.max_history_tokens = max_history_tokens
        self.compact_tool_result_chars = compact_tool_result_chars
        self.context_store = context_store or ContextStore()
        self.telemetry = telemetry or AgentTelemetry()
        self.messages: list[dict] = []

    def _request_messages(self) -> list[dict]:
        return [{"role": "system", "content": self.system_prompt}, *self.messages]

    def _finish_with_assistant(self, content: str) -> str:
        self.messages.append({"role": "assistant", "content": content})
        self.messages = trim_history(self.messages, self.max_history_tokens)
        return content

    def _summary_compact(self) -> bool:
        older_messages, latest_exchange = split_latest_exchange(self.messages)
        if not older_messages:
            return False

        transcript_path = self.context_store.save_transcript(self.messages)
        response = _call_llm(
            self.client,
            self.model,
            _compact_summary_request(older_messages, str(transcript_path)),
            None,
            self.extra_body,
            self.telemetry,
            operation="context_summary",
        )
        summary = response.choices[0].message.content
        if not isinstance(summary, str) or not summary.strip():
            return False

        self.messages = [
            compacted_history_message(summary.strip(), transcript_path),
            *latest_exchange,
        ]
        print(f"[context] 對話歷史已摘要，完整 transcript：{transcript_path}")
        return True

    def _prepare_context(self) -> None:
        self.messages = compact_long_tool_results(
            self.messages,
            self.context_store,
            max_chars=self.compact_tool_result_chars,
        )
        if estimate_tokens(self.messages) > self.max_history_tokens:
            try:
                self._summary_compact()
            except Exception as e:
                print(f"[context] 摘要 compact 失敗，改用裁剪：{e}")
        self.messages = trim_history(self.messages, self.max_history_tokens)

    def _recover_context(self) -> None:
        self.messages = compact_long_tool_results(
            self.messages,
            self.context_store,
            max_chars=self.compact_tool_result_chars,
        )
        try:
            self._summary_compact()
        except Exception as e:
            print(f"[context] overflow 摘要 compact 失敗，改用裁剪：{e}")

        recovery_budget = max(self.max_history_tokens // 2, 1)
        self.messages = trim_history(self.messages, recovery_budget)

    def respond(self, user_input: str) -> str:
        extra = self.input_enricher(user_input) if self.input_enricher else ""
        with self.telemetry.start_span(
            "agent.turn",
            {
                "agent.llm.model": self.model,
                "agent.input.enriched": bool(extra),
            },
        ):
            self.messages.append({"role": "user", "content": user_input + extra})

            tool_rounds = 0
            context_retries = 0
            while True:
                self._prepare_context()
                try:
                    response = _call_llm(
                        self.client,
                        self.model,
                        self._request_messages(),
                        self.tool_schemas or None,
                        self.extra_body,
                        self.telemetry,
                        operation="respond",
                    )
                except ContextWindowExceeded:
                    if context_retries >= _MAX_CONTEXT_RECOVERY_RETRIES:
                        raise
                    self._recover_context()
                    context_retries += 1
                    self.telemetry.record_llm_retry(
                        operation="respond",
                        error_type="context_window",
                    )
                    print("[context] LLM 拒收目前歷史，compact 後重試")
                    continue

                message = response.choices[0].message
                tool_calls = _function_tool_calls(message)

                if tool_calls:
                    self.telemetry.trace_tool_routing(
                        [call.function.name for call in tool_calls],
                        accepted=tool_rounds < self.max_tool_rounds,
                    )

                # 上限判斷必須在 append tool_calls 前做，避免歷史留下未配對結果。
                if tool_calls and tool_rounds >= self.max_tool_rounds:
                    print(
                        "[warn] 單輪 tool call 達到上限 "
                        f"{self.max_tool_rounds}，強制跳出"
                    )
                    return self._finish_with_assistant(_TOOL_ROUND_LIMIT_MESSAGE)

                self.messages.append(_assistant_message(message, tool_calls))
                if not tool_calls:
                    self.messages = trim_history(self.messages, self.max_history_tokens)
                    return message.content or ""

                tool_rounds += 1
                self.messages.extend(
                    _execute_tool_calls(
                        tool_calls,
                        self.tool_handlers,
                        self.telemetry,
                    )
                )
