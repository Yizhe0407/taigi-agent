import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import tiktoken

from agent.diagnostics import log_diagnostic

# 對話歷史保留的 token 上限
# 估算：system prompt ~400 + tools schema ~500 + response buffer ~1000 = ~1900
# 剩餘 budget 留給歷史，設 4000 讓短對話多保留、長回應快被截
MAX_HISTORY_TOKENS = 4000

# Kiosk sessions are short by design; hard-cap at this many user exchanges
# so we never need an LLM-based summary compact.
MAX_EXCHANGES = 5
LONG_TOOL_RESULT_CHARS = 12_000
TOOL_RESULT_PREVIEW_CHARS = 1_500
_TOOL_RESULT_COMPACT_MARKER = "[長工具結果已壓縮]"

# cl100k_base 詞表含常用中文字，1 字通常對應 1 token，對 Qwen 模型是合理近似
_enc = tiktoken.get_encoding("cl100k_base")

# Per-message token count cache keyed by id(msg). Avoids re-encoding the full
# history on every LLM turn (O(N²) → O(N) amortised). id reuse after GC is
# safe in practice: kiosk sessions are short and bounded by MAX_EXCHANGES.
_TOKEN_CACHE: dict[int, int] = {}


def _count_msg_tokens(msg: dict) -> int:
    key = id(msg)
    if key not in _TOKEN_CACHE:
        content = msg.get("content") or ""
        count = len(_enc.encode(content)) if isinstance(content, str) else 0
        tool_calls = msg.get("tool_calls")
        if tool_calls:
            count += len(_enc.encode(json.dumps(tool_calls, ensure_ascii=False)))
        _TOKEN_CACHE[key] = count + 4
    return _TOKEN_CACHE[key]


class ContextStore:
    """把 compact 後仍需保留的完整內容寫到 session 外。"""

    def __init__(self, root: str | Path = ".agent_state") -> None:
        self.root = Path(root)

    def _new_path(self, directory: str, suffix: str, hint: str) -> Path:
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        safe_hint = "".join(c if c.isalnum() else "-" for c in hint).strip("-")
        filename = f"{timestamp}-{safe_hint or 'context'}-{uuid4().hex[:8]}{suffix}"
        path = self.root / directory / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def save_tool_result(self, call_id: str, content: str) -> Path:
        """保存被 context 壓縮的完整 tool result。"""
        path = self._new_path("tool-results", ".txt", call_id)
        path.write_text(content, encoding="utf-8")
        return path


def estimate_tokens(messages: list) -> int:
    """用 tiktoken cl100k_base 估算 messages 的 token 數

    Qwen 有自己的詞表，與 cl100k_base 不完全相同，但對中文的對應結果接近。
    目的是 context 管理而非計費，這個精度已足夠。
    每則訊息加 4 token 的固定 overhead（role / 格式符）。
    """
    return max(sum(_count_msg_tokens(msg) for msg in messages), 1)


def group_exchanges(messages: list) -> list[list]:
    """依 user turn 分組，保留同一輪 assistant tool call/result 配對。"""
    exchanges: list[list] = []
    current: list = []
    for msg in messages:
        if msg["role"] == "user" and current:
            exchanges.append(current)
            current = [msg]
        else:
            current.append(msg)
    if current:
        exchanges.append(current)
    return exchanges


def compact_long_tool_results(
    messages: list[dict],
    store: ContextStore,
    max_chars: int = LONG_TOOL_RESULT_CHARS,
    preview_chars: int = TOOL_RESULT_PREVIEW_CHARS,
) -> list[dict]:
    """長 tool result 落盤，history 只留路徑與預覽。"""
    compacted = list(messages)
    for i, msg in enumerate(compacted):
        if msg.get("role") != "tool":
            continue

        content = msg.get("content")
        if not isinstance(content, str) or len(content) <= max_chars or content.startswith(_TOOL_RESULT_COMPACT_MARKER):
            continue

        msg = dict(msg)
        compacted[i] = msg
        path = store.save_tool_result(str(msg.get("tool_call_id", "tool")), content)
        preview = content[:preview_chars]
        suffix = "\n..." if len(content) > preview_chars else ""
        msg["content"] = f"{_TOOL_RESULT_COMPACT_MARKER}\n完整內容已保存：{path}\n預覽：\n{preview}{suffix}"

    return compacted


def trim_history(
    messages: list[dict],
    max_history_tokens: int = MAX_HISTORY_TOKENS,
    max_exchanges: int = MAX_EXCHANGES,
) -> list:
    """截斷過長的對話歷史，使 token 估算值維持在 max_history_tokens 以內，
    且輪數不超過 max_exchanges。

    仍以「輪」為單位截斷（user msg 起頭到下一個 user msg 前），
    確保 tool_call_id 配對完整，避免 API 報錯。
    """
    exchanges = group_exchanges(messages)

    # Exchange-count cap — apply before token budget so the bound is hard.
    if len(exchanges) > max_exchanges:
        dropped_count = len(exchanges) - max_exchanges
        log_diagnostic("context", f"截掉 {dropped_count} 輪舊對話（上限 {max_exchanges} 輪）")
        exchanges = exchanges[-max_exchanges:]

    # 從最新輪往前累加，超過 budget 就停
    kept: list[list] = []
    tokens_used = 0
    for exchange in reversed(exchanges):
        exchange_tokens = estimate_tokens(exchange)
        # 至少保留一輪（即使單輪就超 budget）
        if tokens_used + exchange_tokens > max_history_tokens and kept:
            break
        kept.insert(0, exchange)
        tokens_used += exchange_tokens

    dropped = len(exchanges) - len(kept)
    if dropped > 0:
        log_diagnostic(
            "context",
            f"截掉 {dropped} 輪舊對話（token 超限），保留約 {tokens_used} tokens",
        )

    return [msg for exchange in kept for msg in exchange]
