"""Harness 核心：messages + LLM call + tool-call loop + context recovery.

只負責 orchestration。其他關注點各自獨立：
  * `agent.llm_client.call_llm`         — chat-completion 呼叫 / 重試 / context 偵測
  * `agent.tool_dispatch.execute_tool_calls` — tool call 結果 / telemetry
  * `agent.error.summarize_error`       — 對外可讀的錯誤摘要
  * `agent.context`                     — token budget、transcript、compact 規則
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from agent.context import (
    LONG_TOOL_RESULT_CHARS,
    MAX_HISTORY_TOKENS,
    compact_long_tool_results,
    trim_history,
)
from agent.diagnostics import log_diagnostic
from agent.llm_client import ContextWindowExceeded, ToolCallFailed, call_llm
from agent.router import ConvState, Decision, Intent, IntentRouter
from agent.tool_dispatch import (
    ToolHandler,
    assistant_message,
    execute_tool_calls,
    function_tool_calls,
)
from pipeline.normalize import normalize_llm_output
from telemetry import AgentTelemetry

InputEnricher = Callable[[str], Awaitable[str]]


def _find_direct_response(tool_calls: list, tool_results: list[dict]) -> str | None:
    """Return the respond_directly message if the model called that tool, else None."""
    for call, result in zip(tool_calls, tool_results):
        if call.function.name == "respond_directly":
            return result["content"]
    return None


_MAX_CONTEXT_RECOVERY_RETRIES = 1
_DEFAULT_MAX_TOOL_ROUNDS = 8
_TOOL_ROUND_LIMIT_MESSAGE = "查詢逾時，請換個方式再問一次。"


@dataclass(frozen=True)
class _LlmTurnResult:
    """LLM loop 的單一收斂出口；`error` 非 None 時由呼叫端記錄 metric 後重拋。"""

    outcome: str  # "ok" | "tool_round_limit" | "error"
    reply: str
    tool_rounds: int
    error: Exception | None = None


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
        telemetry: AgentTelemetry | None = None,
        router: IntentRouter | None = None,
    ) -> None:
        self.client = client
        self.model = model
        self.system_prompt = system_prompt
        self.tool_schemas = tool_schemas
        self.tool_handlers = tool_handlers
        self.input_enricher = input_enricher
        self.extra_body = extra_body if extra_body is not None else {}
        self.max_tool_rounds = max_tool_rounds
        self.max_history_tokens = max_history_tokens
        self.compact_tool_result_chars = compact_tool_result_chars
        self.telemetry = telemetry or AgentTelemetry()
        self.router = router or IntentRouter()
        self.messages: list[dict] = []
        self.conv_state = ConvState()

    def _request_messages(self) -> list[dict]:
        return [{"role": "system", "content": self.system_prompt}, *self.messages]

    def _trim(self) -> None:
        self.messages = trim_history(self.messages, self.max_history_tokens)

    def _finish_with_assistant(self, content: str) -> str:
        self.messages.append({"role": "assistant", "content": content})
        self._trim()
        return content

    def _finish_normalized(self, content: str) -> str:
        """Trim history and return normalized model output.

        Shared by the two LLM exit paths — a free-text completion and a
        respond_directly result — which finalize the turn identically.
        """
        self._trim()
        return normalize_llm_output(content)

    def _compact_and_trim(self, budget: int) -> None:
        self.messages = compact_long_tool_results(
            self.messages,
            max_chars=self.compact_tool_result_chars,
        )
        self.messages = trim_history(self.messages, budget)

    async def _prepare_context(self) -> None:
        self._compact_and_trim(self.max_history_tokens)

    async def _recover_context(self) -> None:
        self._compact_and_trim(max(self.max_history_tokens // 2, 1))

    def _record_canned_turn(self, user_input: str, decision: Decision) -> None:
        """Append user + canned-assistant turn and apply state update.

        Used when the router resolves a turn without an LLM call. Bypasses
        input_enricher (prefetch makes no sense if we already have a final
        answer) and tool dispatch entirely.
        """
        assert decision.canned_response is not None
        self.messages.append({"role": "user", "content": user_input})
        self.messages.append({"role": "assistant", "content": decision.canned_response})
        self._trim()
        if decision.next_state is not None:
            self.conv_state = decision.next_state

    async def respond(self, user_input: str) -> str:
        # Router gate: canned-response intents (Rules 1-3) short-circuit
        # before any LLM cost. Everything else goes to the LLM loop.
        decision = self.router.classify(user_input, self.conv_state)

        if decision.canned_response is not None:
            with self.telemetry.start_span(
                "agent.turn",
                {
                    "agent.router.intent": decision.intent.value,
                    "agent.router.path": "canned",
                },
            ) as span:
                self._record_canned_turn(user_input, decision)
                self.telemetry.set_content(span, "agent.input.text", user_input)
                self.telemetry.set_content(span, "agent.reply.text", decision.canned_response)
                self.telemetry.record_turn(
                    path="canned",
                    intent=decision.intent.value,
                    outcome="ok",
                )
                return decision.canned_response

        return await self._llm_respond(user_input, intent=decision.intent)

    async def _llm_respond(
        self,
        user_input: str,
        *,
        intent: Intent = Intent.UNKNOWN,
    ) -> str:
        extra = await self.input_enricher(user_input) if self.input_enricher else ""
        enriched_content = user_input + extra
        with self.telemetry.start_span(
            "agent.turn",
            {
                "agent.llm.model": self.model,
                "agent.input.enriched": bool(extra),
                "agent.router.intent": intent.value,
                "agent.router.path": "llm",
            },
        ) as span:
            self.telemetry.set_content(span, "agent.input.text", enriched_content)
            self.telemetry.set_content(span, "agent.system_prompt", self.system_prompt)
            self.messages.append({"role": "user", "content": enriched_content})

            result = await self._run_llm_loop()
            # 唯一記錄點：loop 的每個出口（含錯誤）都以 _LlmTurnResult 收斂，
            # 新增出口路徑時型別上必須給 outcome，不可能漏記。
            self.telemetry.record_turn(
                path="llm",
                intent=intent.value,
                outcome=result.outcome,
                tool_rounds=result.tool_rounds,
            )
            if result.error is not None:
                raise result.error
            self.telemetry.set_content(span, "agent.reply.text", result.reply)
            return result.reply

    async def _run_llm_loop(self) -> _LlmTurnResult:
        """LLM tool-call loop。所有出口都回傳 `_LlmTurnResult`，不直接 raise —
        例外帶在 `error` 欄位由 `_llm_respond` 統一記錄 metric 後重拋。"""
        tool_rounds = 0
        context_retries = 0
        force_required = True  # degraded to False on ToolCallFailed
        try:
            while True:
                await self._prepare_context()
                try:
                    response = await call_llm(
                        self.client,
                        self.model,
                        self._request_messages(),
                        self.tool_schemas or None,
                        self.extra_body,
                        self.telemetry,
                        operation="respond",
                        # First round: force a tool call (prevents free-text hallucinations).
                        # Subsequent rounds: allow free text so the model can respond after
                        # receiving tool results without being forced into respond_directly.
                        tool_choice="required" if (tool_rounds == 0 and force_required) else "auto",
                    )
                except ContextWindowExceeded:
                    if context_retries >= _MAX_CONTEXT_RECOVERY_RETRIES:
                        raise
                    await self._recover_context()
                    context_retries += 1
                    self.telemetry.record_llm_retry(
                        operation="respond",
                        error_type="context_window",
                    )
                    log_diagnostic("context", "LLM 拒收目前歷史，compact 後重試")
                    continue
                except ToolCallFailed:
                    if not force_required:
                        raise  # already fell back to auto, still failing
                    force_required = False
                    self.telemetry.record_llm_retry(
                        operation="respond",
                        error_type="tool_call_failed",
                    )
                    log_diagnostic("warn", "tool_call_failed，改用 auto 模式重試")
                    continue

                message = response.choices[0].message
                tool_calls = function_tool_calls(message)

                if tool_calls:
                    self.telemetry.trace_tool_routing(
                        [call.function.name for call in tool_calls],
                        accepted=tool_rounds < self.max_tool_rounds,
                    )

                # 上限判斷必須在 append tool_calls 前做，避免歷史留下未配對結果。
                if tool_calls and tool_rounds >= self.max_tool_rounds:
                    log_diagnostic(
                        "warn",
                        f"單輪 tool call 達到上限 {self.max_tool_rounds}，強制跳出",
                    )
                    return _LlmTurnResult(
                        outcome="tool_round_limit",
                        reply=self._finish_with_assistant(_TOOL_ROUND_LIMIT_MESSAGE),
                        tool_rounds=tool_rounds,
                    )

                self.messages.append(assistant_message(message, tool_calls))
                if not tool_calls:
                    return _LlmTurnResult(
                        outcome="ok",
                        reply=self._finish_normalized(message.content or ""),
                        tool_rounds=tool_rounds,
                    )

                tool_rounds += 1
                tool_results = await execute_tool_calls(
                    tool_calls,
                    self.tool_handlers,
                    self.telemetry,
                )
                self.messages.extend(tool_results)

                # respond_directly short-circuits the loop: no further LLM call needed.
                direct = _find_direct_response(tool_calls, tool_results)
                if direct is not None:
                    return _LlmTurnResult(
                        outcome="ok",
                        reply=self._finish_normalized(direct),
                        tool_rounds=tool_rounds,
                    )
        except Exception as error:
            return _LlmTurnResult(
                outcome="error",
                reply="",
                tool_rounds=tool_rounds,
                error=error,
            )
