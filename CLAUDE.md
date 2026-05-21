# CLAUDE.md

雲林公車台語語音助理（大學專題）。Agent harness 架構：one loop + tools + prompt = agent。

## 指令

```bash
uv sync                      # 安裝依賴
uv run python main.py        # 啟動 CLI
uv run pytest                # 跑測試
uv run ruff check .          # Lint
```

## 固定約束

- **Python 套件**：`uv`，禁用 pip / poetry / conda
- **Session 核心不放領域邏輯**：`agent/session.py` 只處理 messages、LLM call、tool dispatch、context recovery。公車 prefetch 與 provider 規則留在 `tools/`，由入口注入
- **工具回傳 str**：tool handler 必須回傳 `str`，loop.py 直接把結果傳給 LLM
- **解釋義務**：每次寫完或修改 code，必須說明「這在做什麼、為什麼這樣寫、有什麼坑」。沒有解釋 = 沒有完成
- **文件更新義務**：修改 code 後自行判斷是否需要更新文件（CLAUDE.md 目錄結構 / Gotcha / 技術債、TASKS.md 進度、README.md）。判斷依據：新增工具 → 更新 CLAUDE.md 目錄結構；功能補齊 → 更新 TASKS.md；使用方式改變 → 更新 README.md

## 目錄結構

```
main.py            # 入口：load_dotenv() → agent.loop.run()
agent/
  loop.py          # CLI I/O：讀 input、印回答，呼叫 AgentSession
  session.py       # Harness 核心：messages + LLM call + tool-call loop + recovery
  prompt.py        # build_system_prompt()，system prompt 組裝
  tools.py         # TOOL_SCHEMAS（告訴 LLM 有哪些工具）+ TOOL_HANDLERS（名稱→函式）
  context.py       # trim_history()：token budget 截斷（tiktoken 計數），防 context window 爆炸
tools/
  kiosk_bus.py     # Kiosk facade：單一路線 / 本站概況 / 站序工具，處理站名縮寫與 KIOSK_STOP
  yunlin_ebus.py   # 雲林公車資料源：用站牌停靠清單解析 route id，查到站 / ETA 概況 / 站序 / 停靠路線
```

## 加新工具的步驟

1. 在 `tools/` 下建或找對應的 `.py`，寫函式，回傳 `str`
2. 在 `agent/tools.py` 加 import
3. 在 `TOOL_SCHEMAS` 加一筆 OpenAI function calling 格式的 schema
4. 在 `TOOL_HANDLERS` 加 `"函式名": 函式` 的對應
5. `AgentSession` 不加公車專屬分支；若需輸入預取，從入口注入 `input_enricher`

## 已知技術債

- **ebus 後端介面不是本專題控制的公開契約**：目前雲林站牌資料覆蓋足夠，provider 存取集中在 `tools/yunlin_ebus.py`。若 endpoint 或 payload 改版，先修這層，不把格式假設散到 agent loop。
- **Context recovery 目前仍是裁剪式**：`AgentSession` 在每次 LLM call 前 trim history，遇到 context overflow 會以更小 budget 重試一次；尚未做 transcript 保存、LLM 摘要 compact 或長工具輸出落盤。

## 已知 Gotcha

- **route lookup 是 stop-scoped**：`get_arrivals_here` 與 `get_route_stops` 先從 `KIOSK_STOP` 的 `/api/stop/route` 找 route id。沒停靠本站的 route 不會用全縣 route 清單硬查，避免同名 route 歧義
- **站名縮寫要人工處理**：`"雲科大" in "雲林科技大學"` 是 `False`（非連續子字串）。縮寫對照表在 `tools/kiosk_bus.py` 的 `_ALIASES`
- **tool_call_id 配對**：截斷 messages 必須以「輪」為單位，不能直接 `messages[-N:]`，否則 tool_call_id 沒有對應的 tool result，API 報錯
- **tool round limit 也要保配對**：達到上限時不可先把新的 assistant `tool_calls` append 進 history 再跳出，否則下一輪送 API 會缺對應 tool result
- **load_dotenv() 必須在所有 import 之前**：Python import 時執行 module 層級 code，太晚 load 的話 os.getenv() 已經跑過，讀不到 .env
- **vLLM 需要額外啟動參數才能使用 tool calling 與非思考模式**：缺少以下參數時 tool calling 回 400，`enable_thinking: false` 不生效
  ```
  --enable-auto-tool-choice --tool-call-parser hermes --reasoning-parser qwen3
  ```
- **非思考模式 extra_body 格式**：正確是 `{"chat_template_kwargs": {"enable_thinking": False}}`，不是 `{"enable_thinking": False}`（直接傳外層不被 vLLM 識別）

## Commit 規範

Conventional Commits：
- `feat(tools):` 新增工具
- `fix(tools):` 修 bug
- `feat(agent):` 修改 harness 核心
- `docs:` 文件
