from agent.context import compact_long_tool_results, trim_history


def test_trim_history_keeps_latest_tool_exchange_whole():
    old_exchange = [
        {"role": "user", "content": "舊問題 " * 1000},
        {"role": "assistant", "content": "舊回答"},
    ]
    latest_exchange = [
        {"role": "user", "content": "新問題"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "lookup", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": "工具結果"},
        {"role": "assistant", "content": "新回答"},
    ]

    assert trim_history(old_exchange + latest_exchange, max_history_tokens=200) == latest_exchange


def test_compact_long_tool_results_truncates_to_preview():
    content = "站牌資料" * 20
    messages = [
        {"role": "assistant", "content": None, "tool_calls": []},
        {"role": "tool", "tool_call_id": "route-stops", "content": content},
    ]

    compacted = compact_long_tool_results(messages, max_chars=10, preview_chars=8)

    tool_result = compacted[1]["content"]
    assert tool_result.startswith("[長工具結果已截斷]")
    assert "站牌資料站牌資料" in tool_result  # 8-char preview
    assert tool_result.endswith("...")  # truncation marker
    assert len(tool_result) < len(content)
    # 原 messages 不被就地修改（回傳新 list）
    assert messages[1]["content"] == content


def test_compact_long_tool_results_skips_already_compacted():
    content = "[長工具結果已截斷]\n預覽：\n站牌資料"
    messages = [{"role": "tool", "tool_call_id": "x", "content": content}]

    compacted = compact_long_tool_results(messages, max_chars=5, preview_chars=4)

    assert compacted[0]["content"] == content  # marker 前綴 → 不二次截斷
