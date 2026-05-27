import json
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import tiktoken

from agent.diagnostics import log_diagnostic

# 對話歷史保留的 token 上限
# 估算：system prompt ~400 + tools schema ~500 + response buffer ~1000 = ~1900
# 剩餘 budget 留給歷史，設 4000 讓短對話多保留、長回應快被截
MAX_HISTORY_TOKENS = 4000
LONG_TOOL_RESULT_CHARS = 12_000
TOOL_RESULT_PREVIEW_CHARS = 1_500
_TOOL_RESULT_COMPACT_MARKER = "[長工具結果已壓縮]"

# cl100k_base 詞表含常用中文字，1 字通常對應 1 token，對 Qwen 模型是合理近似
_enc = tiktoken.get_encoding("cl100k_base")


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

    def save_transcript(self, messages: list[dict]) -> Path:
        """保存 compact 前的完整 message transcript。"""
        path = self._new_path("transcripts", ".jsonl", "session")
        lines = [json.dumps(msg, ensure_ascii=False) for msg in messages]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
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
    total = 0
    for msg in messages:
        content = msg.get("content") or ""
        if isinstance(content, str):
            total += len(_enc.encode(content))
        tool_calls = msg.get("tool_calls")
        if tool_calls:
            total += len(_enc.encode(json.dumps(tool_calls, ensure_ascii=False)))
        total += 4
    return max(total, 1)


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


def split_latest_exchange(messages: list) -> tuple[list[dict], list[dict]]:
    """回傳較舊歷史與最新 exchange，summary compact 只取代前者。"""
    exchanges = group_exchanges(messages)
    if len(exchanges) <= 1:
        return [], messages
    older = [msg for exchange in exchanges[:-1] for msg in exchange]
    return older, exchanges[-1]


def compact_long_tool_results(
    messages: list[dict],
    store: ContextStore,
    max_chars: int = LONG_TOOL_RESULT_CHARS,
    preview_chars: int = TOOL_RESULT_PREVIEW_CHARS,
) -> list[dict]:
    """長 tool result 落盤，history 只留路徑與預覽。"""
    compacted = deepcopy(messages)
    for msg in compacted:
        if msg.get("role") != "tool":
            continue

        content = msg.get("content")
        if (
            not isinstance(content, str)
            or len(content) <= max_chars
            or content.startswith(_TOOL_RESULT_COMPACT_MARKER)
        ):
            continue

        path = store.save_tool_result(str(msg.get("tool_call_id", "tool")), content)
        preview = content[:preview_chars]
        suffix = "\n..." if len(content) > preview_chars else ""
        msg["content"] = (
            f"{_TOOL_RESULT_COMPACT_MARKER}\n"
            f"完整內容已保存：{path}\n"
            f"預覽：\n{preview}{suffix}"
        )

    return compacted


def compacted_history_message(summary: str, transcript_path: Path) -> dict:
    """建立可放回 conversation history 的摘要 message。"""
    return {
        "role": "user",
        "content": (
            "[先前對話已壓縮]\n"
            f"完整 transcript：{transcript_path}\n"
            f"摘要：\n{summary}"
        ),
    }


def trim_history(
    messages: list[dict], max_history_tokens: int = MAX_HISTORY_TOKENS
) -> list:
    """截斷過長的對話歷史，使 token 估算值維持在 MAX_HISTORY_TOKENS 以內

    仍以「輪」為單位截斷（user msg 起頭到下一個 user msg 前），
    確保 tool_call_id 配對完整，避免 API 報錯。

    token budget 相較於 exchange count 的好處：
    - 短對話（「好」「謝謝」）不浪費配額
    - 含大量站牌的長回應較快被淘汰，不佔用過多 context
    """
    exchanges = group_exchanges(messages)

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
            f"截掉 {dropped} 輪舊對話，保留約 {tokens_used} tokens",
        )

    return [msg for exchange in kept for msg in exchange]
