"""Probe script: test LLM endpoint for Chinese chat + tool calling.

Usage:
    uv run python probe_llm.py            # Groq (GROQ_API_KEY)
    uv run python probe_llm.py --local    # local LLM_BASE_URL endpoint
    uv run python probe_llm.py --nvidia   # NVIDIA NIM (NVIDIA_API_KEY)

Reports response content and any tool calls for each test case.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time

from dotenv import load_dotenv
from openai import AsyncOpenAI

load_dotenv()

from agent.prompt import build_system_prompt

# ── Tool schemas (minimal subset for probe) ───────────────────────────────────

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_arrivals_here",
            "description": "查詢某路線下一班抵達本站的即時到站時間。",
            "parameters": {
                "type": "object",
                "properties": {
                    "route": {"type": "string", "description": "路線號碼，例如 '201'、'7126'。"},
                },
                "required": ["route"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_routes_to_destination",
            "description": "查詢本站有哪些路線能到達指定目的地。",
            "parameters": {
                "type": "object",
                "properties": {
                    "destination": {"type": "string", "description": "目的地名稱。"},
                },
                "required": ["destination"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_stop_arrival_statuses_here",
            "description": "查詢本站目前所有停靠路線的到站狀態。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]

SYSTEM = build_system_prompt()

CASES = [
    ("基本問候（不需工具）", "你好"),
    ("查路線（應呼叫 find_routes_to_destination）", "我想去虎尾科大怎麼搭"),
    ("查到站（應呼叫 get_arrivals_here）", "201幾分鐘後到"),
    ("末班查詢（應呼叫 get_stop_arrival_statuses_here）", "還有車嗎"),
    ("無關查詢（不需工具）", "明天天氣怎樣"),
]


async def probe_one(
    client: AsyncOpenAI, model: str, user_msg: str, extra_body: dict
) -> None:
    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": user_msg},
    ]
    t0 = time.perf_counter()
    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=messages,
            tools=TOOLS,
            extra_body=extra_body or None,
        )
    except Exception as e:
        print(f"  ERROR: {e}")
        return

    elapsed = time.perf_counter() - t0
    msg = resp.choices[0].message
    tool_calls = msg.tool_calls or []

    print(f"  [{elapsed:.1f}s] ", end="")
    if tool_calls:
        for tc in tool_calls:
            args = json.loads(tc.function.arguments)
            print(f"→ tool:{tc.function.name}({args})")
    else:
        print(f"→ text: {(msg.content or '').strip()[:120]}")


async def main() -> None:
    use_local = "--local" in sys.argv
    use_nvidia = "--nvidia" in sys.argv

    if use_local:
        base_url = os.getenv("LLM_BASE_URL", "")
        model = os.getenv("LLM_MODEL", "")
        api_key = os.getenv("LLM_API_KEY", "ollama")
        # vLLM (Qwen3) needs this to suppress chain-of-thought tokens.
        extra_body: dict = {"chat_template_kwargs": {"enable_thinking": False}}
        print(f"=== LOCAL (vLLM): {model} @ {base_url} ===\n")
    elif use_nvidia:
        raw_url = os.getenv("NVIDIA_BASE_URL", "")
        # Strip /chat/completions suffix if present — OpenAI client adds it.
        base_url = raw_url.removesuffix("/chat/completions")
        model = os.getenv("NVIDIA_MODEL", "")
        api_key = os.getenv("NVIDIA_API_KEY", "")
        extra_body = {}
        print(f"=== NVIDIA NIM: {model} @ {base_url} ===\n")
    else:
        base_url = "https://api.groq.com/openai/v1"
        model = os.getenv("GROQ_MODEL", "qwen/qwen3-32b")
        api_key = os.getenv("GROQ_API_KEY", "")
        extra_body = {}
        print(f"=== GROQ: {model} ===\n")

    if not base_url or not model:
        print("Missing env vars. Set GROQ_API_KEY, or use --local (LLM_BASE_URL/LLM_MODEL), or --nvidia (NVIDIA_BASE_URL/NVIDIA_MODEL).")
        sys.exit(1)

    client = AsyncOpenAI(base_url=base_url, api_key=api_key)

    for label, user_msg in CASES:
        print(f"[{label}]")
        print(f"  user: {user_msg}")
        await probe_one(client, model, user_msg, extra_body)
        print()


asyncio.run(main())
