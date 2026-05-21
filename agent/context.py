import json

import tiktoken

# 對話歷史保留的 token 上限
# 估算：system prompt ~400 + tools schema ~500 + response buffer ~1000 = ~1900
# 剩餘 budget 留給歷史，設 4000 讓短對話多保留、長回應快被截
MAX_HISTORY_TOKENS = 4000

# cl100k_base 詞表含常用中文字，1 字通常對應 1 token，對 Qwen 模型是合理近似
_enc = tiktoken.get_encoding("cl100k_base")


def _estimate_tokens(messages: list) -> int:
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


def trim_history(
    messages: list, max_history_tokens: int = MAX_HISTORY_TOKENS
) -> list:
    """截斷過長的對話歷史，使 token 估算值維持在 MAX_HISTORY_TOKENS 以內

    仍以「輪」為單位截斷（user msg 起頭到下一個 user msg 前），
    確保 tool_call_id 配對完整，避免 API 報錯。

    token budget 相較於 exchange count 的好處：
    - 短對話（「好」「謝謝」）不浪費配額
    - 含大量站牌的長回應較快被淘汰，不佔用過多 context
    """
    # 按輪分組
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

    # 從最新輪往前累加，超過 budget 就停
    kept: list[list] = []
    tokens_used = 0
    for exchange in reversed(exchanges):
        exchange_tokens = _estimate_tokens(exchange)
        # 至少保留一輪（即使單輪就超 budget）
        if tokens_used + exchange_tokens > max_history_tokens and kept:
            break
        kept.insert(0, exchange)
        tokens_used += exchange_tokens

    dropped = len(exchanges) - len(kept)
    if dropped > 0:
        print(f"[context] 截掉 {dropped} 輪舊對話，保留約 {tokens_used} tokens")

    return [msg for exchange in kept for msg in exchange]
