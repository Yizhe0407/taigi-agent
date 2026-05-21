from agent.context import trim_history


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
