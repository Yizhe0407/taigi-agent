from agent.context import ContextStore, compact_long_tool_results, trim_history


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

    assert trim_history(
        old_exchange + latest_exchange, max_history_tokens=200
    ) == latest_exchange


def test_compact_long_tool_results_persists_full_content(tmp_path):
    content = "站牌資料" * 20
    messages = [
        {"role": "assistant", "content": None, "tool_calls": []},
        {"role": "tool", "tool_call_id": "route-stops", "content": content},
    ]

    compacted = compact_long_tool_results(
        messages,
        ContextStore(tmp_path),
        max_chars=10,
        preview_chars=8,
    )

    tool_result = compacted[1]["content"]
    assert tool_result.startswith("[長工具結果已壓縮]")
    assert "站牌資料站牌資料" in tool_result
    stored = list((tmp_path / "tool-results").glob("*.txt"))
    assert len(stored) == 1
    assert stored[0].read_text(encoding="utf-8") == content
    assert messages[1]["content"] == content
