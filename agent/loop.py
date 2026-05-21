import json
import os
import re
import sys
import time

from openai import OpenAI

from agent.context import trim_history
from agent.prompt import build_system_prompt
from agent.tools import TOOL_HANDLERS, TOOL_SCHEMAS

# 路線號碼 pattern：Y01 / 101 / 7126 等
# negative lookahead 排除常見非路線用法：101大樓、3號出口、5樓、30分鐘
_ROUTE_RE = re.compile(
    r"\b([A-Za-z]?\d{2,4})\b"
    r"(?!大樓|號出口|出口|樓層|樓|棟|館|分鐘|分|秒|公里|公尺|元|歲)"
)

_LLM_MAX_RETRIES = 3   # LLM API 暫時失敗的最大重試次數
_MAX_TOOL_ROUNDS = 8   # 單輪對話最多幾次 tool call，防止 LLM 無限迴圈


def _call_llm(client, model: str, messages: list, tools: list | None, extra_body: dict):
    """呼叫 LLM，失敗時指數退避 retry。

    為什麼獨立成函式？
    loop 結構不動（learn-claude-code s11 原則），retry 邏輯包在 helper 裡，
    呼叫端看不到重試細節，只知道「成功回傳 response」或「全部失敗拋例外」。
    """
    for attempt in range(_LLM_MAX_RETRIES):
        try:
            return client.chat.completions.create(
                model=model,
                messages=messages,
                tools=tools,
                extra_body=extra_body,
            )
        except Exception as e:
            if attempt == _LLM_MAX_RETRIES - 1:
                raise
            wait = 2 ** attempt  # 1s → 2s → 4s
            print(f"[retry] LLM 呼叫失敗（{e}），{wait}s 後重試…")
            time.sleep(wait)

    raise RuntimeError("LLM retry loop ended unexpectedly")


def _prefetch(user_input: str) -> str:
    """偵測路線號碼，主動查詢本站到站時間並注入 user message。

    Kiosk 模式：stop_name 固定，偵測到路線號就直接查 get_arrivals_here。
    不再需要關鍵字判斷（幾點 / 幾分鐘），任何問法都查本站到站。

    為什麼 pre-dispatch 而不依賴 LLM 呼叫工具？
    Qwen3 等小型模型工具遵從率不穩定，有時用訓練資料直接回答。
    Python 層預取後注入，LLM 只需整理回答格式，不需要決定要不要查工具。
    """
    m = _ROUTE_RE.search(user_input)
    if not m:
        return ""

    route = m.group(1)
    from tools.kiosk_bus import get_arrivals_here
    result = get_arrivals_here(route)
    return (
        "\n\n[工具查詢結果，必須直接使用，禁止用訓練資料替代]\n"
        f"路線 {route} 到本站的資訊：\n{result}"
    )


def run() -> None:
    # 從 env 讀 LLM 設定，在 run() 裡建立 client 而不是 module 層級
    # 為什麼不放 module 層級？module import 時就會執行，
    # 若 load_dotenv() 還沒跑完，os.getenv() 可能讀到空值
    # 放在 run() 裡確保 main.py 的 load_dotenv() 一定先跑完
    base_url = os.getenv("LLM_BASE_URL")
    model = os.getenv("LLM_MODEL")
    api_key = os.getenv("LLM_API_KEY", "ollama")

    if not base_url or not model:
        print("錯誤：請在 .env 設定 LLM_BASE_URL 和 LLM_MODEL")
        sys.exit(1)

    client = OpenAI(base_url=base_url, api_key=api_key)

    # messages 存整個對話歷史
    # LLM 每次呼叫都要把完整歷史傳進去，它才知道前面聊了什麼
    messages: list = []
    system_prompt = build_system_prompt()

    print("雲林公車助理啟動（輸入 'exit' 結束）\n")

    # 外層迴圈：等使用者輸入下一句話
    while True:
        user_input = input("你: ").strip()

        if user_input.lower() in ("exit", "quit", "再見", "掰掰"):
            print("掰掰！")
            break

        if not user_input:
            continue

        # 主動預取：偵測路線號碼就直接查，不等 LLM 決定
        extra = _prefetch(user_input)
        messages.append({"role": "user", "content": user_input + extra})

        # 內層迴圈：agent 思考 + 工具呼叫
        # 為什麼要內層迴圈？
        # 因為 agent 可能需要多次工具呼叫才能回答一個問題
        # 每次工具呼叫完就繼續這個迴圈，直到 LLM 決定「我可以直接回答了」
        tool_rounds = 0
        while True:
            try:
                response = _call_llm(
                    client, model,
                    messages=[{"role": "system", "content": system_prompt}] + messages,
                    tools=TOOL_SCHEMAS or None,
                    extra_body={"chat_template_kwargs": {"enable_thinking": False}},
                )
            except Exception as e:
                print("\n助理: 系統暫時無法回應，請稍後再試。\n")
                print(f"[error] LLM 呼叫失敗：{e}")
                break

            message = response.choices[0].message
            function_tool_calls = [
                call for call in message.tool_calls or [] if call.type == "function"
            ]

            # 把 assistant 的回覆加到歷史
            # 為什麼不管有沒有 tool call 都要加？
            # 因為下一輪呼叫 LLM 時，它需要看到自己說過什麼
            if function_tool_calls:
                messages.append({
                    "role": "assistant",
                    "content": message.content,
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments,
                            },
                        }
                        for tc in function_tool_calls
                    ],
                })
            else:
                messages.append({"role": "assistant", "content": message.content})

            # 沒有 tool_calls → LLM 決定直接回答 → 跳出內層迴圈
            if not function_tool_calls:
                print(f"\n助理: {message.content}\n")
                # 一輪對話結束，截斷過長的歷史
                # 為什麼在這裡截，不在 LLM 呼叫前截？
                # 因為這一輪的 tool calls + results 都已經完整，
                # 現在截才不會把半完整的輪次切斷
                messages = trim_history(messages)
                break

            # tool_rounds 超過上限：LLM 卡在 tool-call 迴圈，強制跳出
            tool_rounds += 1
            if tool_rounds >= _MAX_TOOL_ROUNDS:
                print("\n助理: 查詢逾時，請換個方式再問一次。\n")
                print(f"[warn] 單輪 tool call 達到上限 {_MAX_TOOL_ROUNDS}，強制跳出")
                break

            # 有 tool_calls → 逐一執行工具 → 把結果傳回給 LLM → 繼續內層迴圈
            tool_results = []
            for call in function_tool_calls:
                tool_name = call.function.name

                try:
                    tool_args = json.loads(call.function.arguments)
                except json.JSONDecodeError:
                    tool_results.append({
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": f"錯誤：工具參數格式有誤，無法執行 {tool_name}",
                    })
                    continue

                handler = TOOL_HANDLERS.get(tool_name)
                if handler:
                    try:
                        result = handler(**tool_args)
                    except Exception as e:
                        result = f"工具 {tool_name} 執行失敗：{e}"
                else:
                    result = f"錯誤：找不到工具 {tool_name}"

                # tool_call_id 必須對應到 assistant message 裡面那個 call 的 id
                # LLM 用這個 id 知道「這個工具結果是回答我哪一個問題的」
                tool_results.append({
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": str(result),
                })

            messages.extend(tool_results)
