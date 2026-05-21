import json
import time
from collections.abc import Callable, Mapping
from typing import Any

from agent.context import MAX_HISTORY_TOKENS, trim_history

InputEnricher = Callable[[str], str]
ToolHandler = Callable[..., str]

_LLM_MAX_RETRIES = 3
_MAX_CONTEXT_RECOVERY_RETRIES = 1
_DEFAULT_MAX_TOOL_ROUNDS = 8
_TOOL_ROUND_LIMIT_MESSAGE = "查詢逾時，請換個方式再問一次。"
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
        self.messages: list[dict] = []

    def _request_messages(self) -> list[dict]:
        return [{"role": "system", "content": self.system_prompt}, *self.messages]

    def _finish_with_assistant(self, content: str) -> str:
        self.messages.append({"role": "assistant", "content": content})
        self.messages = trim_history(self.messages)
        return content

    def respond(self, user_input: str) -> str:
        extra = self.input_enricher(user_input) if self.input_enricher else ""
        self.messages.append({"role": "user", "content": user_input + extra})

        tool_rounds = 0
        context_retries = 0
        while True:
            # 每次 LLM call 前先丟掉舊 exchange，避免長會話撞上 context limit。
            self.messages = trim_history(self.messages)
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
                self.messages = trim_history(
                    self.messages,
                    max_history_tokens=max(MAX_HISTORY_TOKENS // 2, 1),
                )
                context_retries += 1
                print("[context] LLM 拒收目前歷史，縮減 token budget 後重試")
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
                self.messages = trim_history(self.messages)
                return message.content or ""

            tool_rounds += 1
            self.messages.extend(_execute_tool_calls(tool_calls, self.tool_handlers))
