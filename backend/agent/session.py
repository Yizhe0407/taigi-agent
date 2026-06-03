"""Harness 核心：messages + LLM call + tool-call loop + context recovery.

只負責 orchestration。其他關注點各自獨立：
  * `agent.llm_client.call_llm`         — chat-completion 呼叫 / 重試 / context 偵測
  * `agent.tool_dispatch.execute_tool_calls` — tool call 結果 / telemetry
  * `agent.error.summarize_error`       — 對外可讀的錯誤摘要
  * `agent.context`                     — token budget、transcript、compact 規則
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from agent.context import (
    LONG_TOOL_RESULT_CHARS,
    MAX_HISTORY_TOKENS,
    ContextStore,
    compact_long_tool_results,
    trim_history,
)
from agent.diagnostics import log_diagnostic
from agent.llm_client import ContextWindowExceeded, call_llm
from agent.router import ConvState, Decision, Intent, IntentRouter
from agent.telemetry import AgentTelemetry
from agent.tool_dispatch import (
    ToolHandler,
    assistant_message,
    execute_tool_calls,
    function_tool_calls,
)

InputEnricher = Callable[[str], Awaitable[str]]

_MAX_CONTEXT_RECOVERY_RETRIES = 1
_DEFAULT_MAX_TOOL_ROUNDS = 8
_TOOL_ROUND_LIMIT_MESSAGE = "查詢逾時，請換個方式再問一次。"


def _phrase_tool_result(intent: Intent, result: str, tool_kwargs: dict) -> str:
    """Template a raw tool result into a kiosk-style reply."""
    if intent == Intent.ARRIVAL_TIME:
        return result

    if intent == Intent.FIND_ROUTES_TO_DEST:
        if any(kw in result for kw in ("失敗", "找不到", "沒有直達")):
            return result
        dest = tool_kwargs.get("destination", "")
        if "、" in result:
            # Multiple routes — keep direction labels so user can tell them apart.
            return f"去{dest}可以搭{result}。"
        parts = result.split(" ", 1)
        route, direction = (parts[0], parts[1]) if len(parts) == 2 else (result, "")
        if direction:
            # "搭7120就可以，往高鐵雲林站那班。" reads more naturally than
            # "搭7120 往高鐵雲林站就可以到虎尾科大。"
            return f"搭{route}就可以，{direction}那班。"
        return f"搭{route}就可以到{dest}。"

    if intent == Intent.OTHER_ROUTES_FOLLOWUP:
        if any(kw in result for kw in ("失敗", "找不到")):
            return result
        if "沒有直達" in result:
            return result
        # Direction labels are already in context from the first route query;
        # strip them to avoid repetition ("就只有7120" not "就只有7120 往高鐵雲林站").
        routes = "、".join(e.split(" ")[0] for e in result.split("、"))
        return f"就只有{routes}，沒有其他路線了。"

    return result


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
        self.context_store = context_store or ContextStore()
        self.telemetry = telemetry or AgentTelemetry()
        self.router = router or IntentRouter()
        self.messages: list[dict] = []
        self.conv_state = ConvState()

    def _request_messages(self) -> list[dict]:
        return [{"role": "system", "content": self.system_prompt}, *self.messages]

    def _finish_with_assistant(self, content: str) -> str:
        self.messages.append({"role": "assistant", "content": content})
        self.messages = trim_history(self.messages, self.max_history_tokens)
        return content

    async def _prepare_context(self) -> None:
        self.messages = compact_long_tool_results(
            self.messages,
            self.context_store,
            max_chars=self.compact_tool_result_chars,
        )
        self.messages = trim_history(self.messages, self.max_history_tokens)

    async def _recover_context(self) -> None:
        self.messages = compact_long_tool_results(
            self.messages,
            self.context_store,
            max_chars=self.compact_tool_result_chars,
        )
        recovery_budget = max(self.max_history_tokens // 2, 1)
        self.messages = trim_history(self.messages, recovery_budget)

    def _record_canned_turn(self, user_input: str, decision: Decision) -> None:
        """Append user + canned-assistant turn and apply state update.

        Used when the router resolves a turn without an LLM call. Bypasses
        input_enricher (prefetch makes no sense if we already have a final
        answer) and tool dispatch entirely.
        """
        assert decision.canned_response is not None
        self.messages.append({"role": "user", "content": user_input})
        self.messages.append(
            {"role": "assistant", "content": decision.canned_response}
        )
        self.messages = trim_history(self.messages, self.max_history_tokens)
        if decision.next_state is not None:
            self.conv_state = decision.next_state

    async def _tool_respond(self, user_input: str, decision: Decision) -> str:
        """Execute a router-dispatched tool call without an LLM round-trip."""
        tool_name, tool_kwargs = decision.tool_call  # type: ignore[misc]
        with self.telemetry.start_span(
            "agent.turn",
            {
                "agent.router.intent": decision.intent.value,
                "agent.router.path": "tool",
            },
        ):
            handler = self.tool_handlers.get(tool_name)
            if handler is None:
                result = f"找不到工具 {tool_name}"
            else:
                with self.telemetry.start_span(
                    "agent.tool.call", {"tool.name": tool_name}
                ):
                    t0 = time.monotonic()
                    try:
                        result = await handler(**tool_kwargs)
                        self.telemetry.record_tool_duration(
                            time.monotonic() - t0,
                            tool_name=tool_name,
                            outcome="ok",
                        )
                    except Exception as exc:
                        self.telemetry.record_tool_duration(
                            time.monotonic() - t0,
                            tool_name=tool_name,
                            outcome="error",
                        )
                        self.telemetry.record_tool_error(
                            tool_name=tool_name,
                            error_type=type(exc).__name__,
                        )
                        result = f"工具 {tool_name} 執行失敗"

            reply = _phrase_tool_result(decision.intent, result, tool_kwargs)
            self.messages.append({"role": "user", "content": user_input})
            self._finish_with_assistant(reply)
            if decision.next_state is not None:
                self.conv_state = decision.next_state
            return reply

    async def respond(self, user_input: str) -> str:
        # Router gate: deterministic intents short-circuit before any LLM cost.
        decision = self.router.classify(user_input, self.conv_state)

        if decision.canned_response is not None:
            with self.telemetry.start_span(
                "agent.turn",
                {
                    "agent.router.intent": decision.intent.value,
                    "agent.router.path": "canned",
                },
            ):
                self._record_canned_turn(user_input, decision)
                return decision.canned_response

        if decision.tool_call is not None:
            return await self._tool_respond(user_input, decision)

        # Fall through to legacy LLM loop. As more intents migrate into the
        # router, this branch shrinks. The intent label flows into telemetry
        # so we can monitor migration progress.
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
        ):
            self.messages.append({"role": "user", "content": enriched_content})

            tool_rounds = 0
            context_retries = 0
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
                    return self._finish_with_assistant(_TOOL_ROUND_LIMIT_MESSAGE)

                self.messages.append(assistant_message(message, tool_calls))
                if not tool_calls:
                    self.messages = trim_history(self.messages, self.max_history_tokens)
                    return message.content or ""

                tool_rounds += 1
                self.messages.extend(
                    await execute_tool_calls(
                        tool_calls,
                        self.tool_handlers,
                        self.telemetry,
                    )
                )
