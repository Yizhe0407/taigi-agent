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
- **loop.py 業務邏輯不動**：新功能只加 `tools/` 裡的函式 + 在 `agent/tools.py` 登記；loop.py 只允許修改 LLM call 參數（model、extra_body 等），不加業務邏輯
- **工具回傳 str**：tool handler 必須回傳 `str`，loop.py 直接把結果傳給 LLM
- **解釋義務**：每次寫完或修改 code，必須說明「這在做什麼、為什麼這樣寫、有什麼坑」。沒有解釋 = 沒有完成
- **文件更新義務**：修改 code 後自行判斷是否需要更新文件（CLAUDE.md 目錄結構 / Gotcha / 技術債、TASKS.md 進度、README.md）。判斷依據：新增工具 → 更新 CLAUDE.md 目錄結構；功能補齊 → 更新 TASKS.md；使用方式改變 → 更新 README.md

## 目錄結構

```
main.py            # 入口：load_dotenv() → agent.loop.run()
agent/
  loop.py          # Harness 核心：外層 input loop + 內層 tool-call loop
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
5. loop.py 業務邏輯不用動

## 已知技術債

- **ebus 後端介面不是本專題控制的公開契約**：目前雲林站牌資料覆蓋足夠，provider 存取集中在 `tools/yunlin_ebus.py`。若 endpoint 或 payload 改版，先修這層，不把格式假設散到 agent loop。
- **loop.py 混了 I/O 與 agent 邏輯**：目前 `run()` 同時處理 `input()` / `print()`（CLI I/O）和 agent 核心（prefetch + LLM call + tool dispatch）。等到接 ASR/TTS 時再拆：`agent/session.py` 純 agent 邏輯（輸入輸出都是 str）、`main.py` / `voice_main.py` 各自的 I/O 層。現在拆是過設計。

## 已知 Gotcha

- **route lookup 是 stop-scoped**：`get_arrivals_here` 與 `get_route_stops` 先從 `KIOSK_STOP` 的 `/api/stop/route` 找 route id。沒停靠本站的 route 不會用全縣 route 清單硬查，避免同名 route 歧義
- **站名縮寫要人工處理**：`"雲科大" in "雲林科技大學"` 是 `False`（非連續子字串）。縮寫對照表在 `tools/kiosk_bus.py` 的 `_ALIASES`
- **tool_call_id 配對**：截斷 messages 必須以「輪」為單位，不能直接 `messages[-N:]`，否則 tool_call_id 沒有對應的 tool result，API 報錯
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
