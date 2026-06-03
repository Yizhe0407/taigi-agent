"""End-to-end conversation test against the full agent stack.

Runs a scripted multi-turn conversation through AgentSession (real tools,
real Groq LLM) and prints each exchange. No assertions — judge by reading.

Usage:
    uv run python e2e_test.py
"""

from __future__ import annotations

import asyncio

from dotenv import load_dotenv

load_dotenv()

from config import Settings, make_agent_session  # noqa: E402

TURNS = [
    # ── Canned response path (router, no LLM) ──────────────────────────
    ("201", "Rule 1: 純路線號 → 詢問意圖"),
    ("我要去台中", "Rule 2: 遠程城市 → 地圖提示"),
    ("201路完整時刻表", "Rule 3: 時刻表 → 不支援"),
    # ── Tool call path ──────────────────────────────────────────────────
    ("我想去虎尾科大怎麼搭", "Rule 4: 查路線 → find_routes_to_destination"),
    ("還有其他路線嗎", "Rule 4 follow-up: 用 last_destination 再查"),
    ("7120幾分鐘後到", "Rule 7: 查到站 → get_arrivals_here"),
    ("還有車嗎", "Rule 6: 全站狀態 → get_stop_arrival_statuses_here"),
    # ── Multi-turn: 先問路線，再問到站 ──────────────────────────────────
    ("201有停斗六火車站嗎", "Rule 9: check_stop_on_route"),
    ("這站有哪些公車", "Rule 5: routes_at_stop"),
    # ── UNKNOWN fallback → LLM ─────────────────────────────────────────
    ("你好", "UNKNOWN fallback: 閒聊 → LLM"),
    ("明天天氣怎樣", "UNKNOWN fallback: 不相關 → LLM 一句帶過"),
]


async def main() -> None:
    settings = Settings.from_env()
    print(f"LLM: {settings.llm_model} @ {settings.llm_base_url}\n")
    session = make_agent_session(settings)

    for user_input, label in TURNS:
        print(f"[{label}]")
        print(f"  你: {user_input}")
        reply = await session.respond(user_input)
        print(f"  助理: {reply}")
        print()


asyncio.run(main())
