import json
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


class ContextWindowExceeded(RuntimeError):
    """LLM 拒收目前 messages，需縮減歷史後重試。"""


def _looks_like_context_error(error: Exception) -> bool:
    text = str(error).lower()
    return any(marker in text for marker in _CONTEXT_ERROR_MARKERS)


def _call_llm(
    client: Any,
    model: str,
    messages: list[dict],
    tools: list | None,
    extra_body: dict,
):
    """呼叫 LLM，暫時性錯誤退避重試，context overflow 交回 session 修復。"""
    for attempt in range(_LLM_MAX_RETRIES):
        try:
            return client.chat.completions.create(
                model=model,
                messages=messages,
                tools=tools,
                extra_body=extra_body,
            )
        except Exception as e:
            if _looks_like_context_error(e):
                raise ContextWindowExceeded(str(e)) from e
            if attempt == _LLM_MAX_RETRIES - 1:
                raise
            wait = 2 ** attempt
            print(f"[retry] LLM 呼叫失敗（{e}），{wait}s 後重試...")
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
    tool_calls: list[Any], handlers: Mapping[str, ToolHandler]
) -> list[dict]:
    results = []
    for call in tool_calls:
        tool_name = call.function.name
        try:
            tool_args = json.loads(call.function.arguments or "{}")
        except (TypeError, json.JSONDecodeError):
            results.append(
                _tool_error(
                    call.id,
                    f"錯誤：工具參數格式有誤，無法執行 {tool_name}",
                )
            )
            continue

        if not isinstance(tool_args, dict):
            results.append(
                _tool_error(
                    call.id,
                    f"錯誤：工具參數必須是 JSON object，無法執行 {tool_name}",
                )
            )
            continue

        handler = handlers.get(tool_name)
        if handler is None:
            results.append(_tool_error(call.id, f"錯誤：找不到工具 {tool_name}"))
            continue

        try:
            result = handler(**tool_args)
        except Exception as e:
            result = f"工具 {tool_name} 執行失敗：{e}"

        # tool_call_id 必須對應 assistant message 裡同一個 call id。
        results.append(_tool_error(call.id, str(result)))

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
                )
            except ContextWindowExceeded:
                if context_retries >= _MAX_CONTEXT_RECOVERY_RETRIES:
                    raise
                self._recover_context()
                context_retries += 1
                print("[context] LLM 拒收目前歷史，compact 後重試")
                continue

            message = response.choices[0].message
            tool_calls = _function_tool_calls(message)

            # 上限判斷必須在 append tool_calls 前做，避免歷史留下未配對結果。
            if tool_calls and tool_rounds >= self.max_tool_rounds:
                print(
                    f"[warn] 單輪 tool call 達到上限 {self.max_tool_rounds}，強制跳出"
                )
                return self._finish_with_assistant(_TOOL_ROUND_LIMIT_MESSAGE)

            self.messages.append(_assistant_message(message, tool_calls))
            if not tool_calls:
                self.messages = trim_history(self.messages, self.max_history_tokens)
                return message.content or ""

            tool_rounds += 1
            self.messages.extend(_execute_tool_calls(tool_calls, self.tool_handlers))
