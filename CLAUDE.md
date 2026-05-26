# CLAUDE.md

雲林公車台語語音助理（大學專題）。Agent harness 架構：one loop + tools + prompt = agent。

## 指令

```bash
cd backend
uv sync                      # 安裝依賴
uv run python main.py        # 啟動 CLI
uv run uvicorn api:app --reload --port 8000  # 啟動 route planning API
uv run pytest                # 跑測試
uv run ruff check .          # Lint
```

## 固定約束

- **Python 套件**：`uv`，禁用 pip / poetry / conda
- **Session 核心不放領域邏輯**：`backend/agent/session.py` 只處理 messages、LLM call、tool dispatch、context recovery。公車 prefetch 與 provider 規則留在 `backend/tools/`，由入口注入
- **工具回傳 str**：tool handler 必須回傳 `str`，loop.py 直接把結果傳給 LLM
- **解釋義務**：每次寫完或修改 code，必須說明「這在做什麼、為什麼這樣寫、有什麼坑」。沒有解釋 = 沒有完成
- **文件更新義務**：修改 code 後自行判斷是否需要更新文件（CLAUDE.md 目錄結構 / Gotcha / 技術債、TASKS.md 進度、README.md）。判斷依據：新增工具 → 更新 CLAUDE.md 目錄結構；功能補齊 → 更新 TASKS.md；使用方式改變 → 更新 README.md

## 目錄結構

```
backend/
  main.py          # 入口：load_dotenv() → agent.loop.run()
  config.py        # Settings（all env vars）+ make_agent_session 工廠；API 層與 CLI 共用
  api/
    __init__.py    # FastAPI app 建立 + CORS + include_router（uvicorn api:app）
    chat.py        # /api/chat/* + in-process session store（_chat_sessions）
    departures.py  # /api/departures/here + /api/departures/routes/{route}/detail
    route_plans.py # /api/route-plans + /api/kiosk
    moovo.py       # /api/moovo/*
    asr.py         # /api/asr（Qwen3-ASR proxy）
    tts.py         # /api/tts（HanloFlow → Taibun → Piper TTS proxy）
  agent/
    loop.py        # CLI I/O：讀 input、印回答，呼叫 AgentSession
    session.py     # Harness 核心：messages + LLM call + tool-call loop + recovery
    telemetry.py   # OpenTelemetry spans / metrics；有 OTLP endpoint 才啟用 exporter
    prompt.py      # build_system_prompt()，system prompt 組裝
    tools.py       # TOOL_SCHEMAS + TOOL_HANDLERS
    context.py     # token budget、ContextStore、transcript / 長 tool result compact
  pipeline/
    text_processor.py  # Mandarin → HanloFlow(漢羅) → Taibun(Tailo)；module-level 單例
  tools/
    kiosk_bus.py   # Kiosk facade：單一路線 / 本站概況 / 站序工具
    kiosk_route_planner.py  # Kiosk 固定出發、前端地圖選點目的地的 OTP planner
    otp.py         # OTP GTFS GraphQL provider：BUS planConnection 與 itinerary parser
    stop_catalog.py  # 讀取 TDX / GTFS 更新流程產生的雲林 stop index
    yunlin_ebus.py # 雲林公車 ebus provider
  scripts/
    update_yunlin_gtfs.py  # TDX GTFS 與 stop metadata 更新流程
  otp/
    data/          # 本機 OTP build input / graph；git 只保留資料夾
  tests/
frontend/
  public/avatar.png            # 虛擬站務員小芸半身像（PIP + Hero 按鈕用）
  src/App.vue                  # Kiosk shell：view 狀態（home/planning）+ PipAgentOverlay
  src/features/departures/
    kiosk-data.ts              # 靜態展示資料：ROUTE_COLORS
    api/departures.ts          # 離站決策與路線站序 API client
    composables/useDepartureSnapshot.ts  # 本站離站決策輪詢、abort 與錯誤狀態
    composables/useDepartureRouteDetail.ts  # 路線站序詳情 fetch / abort / error
    utils/departure-status.ts  # route / hero 顯示狀態與等待時間文字
    components/
      DepartureDashboardView.vue   # V4 kiosk 主頁：Hero 大字卡 + 路線列表
      RouteDetailPanel.vue         # 路線詳情：從後端載入真實站點時間軸
      PipAgentOverlay.vue          # PIP 子母畫面虛擬站務員（接 /api/chat）
  src/features/route-planner/  # Vue + MapCN destination picker 與路線規劃
    composables/useRoutePlanner.ts        # 路線規劃頁狀態、API request 與 abort
    composables/useScheduledTimeWheel.ts  # 指定時間 bottom sheet wheel 狀態
    utils/route-display.ts                # route option / leg 顯示文字與顏色
    components/RoutePlannerView.vue  # 全頁路線規劃，emit 'back' → App.vue
  src/features/agent-chat/     # chat API client（PipAgentOverlay 引用 chat.ts）
    composables/usePipChat.ts  # PIP 對話 session / send / scroll 狀態
  src/components/ui/           # shadcn-vue 與 MapCN Vue copy-paste UI components
```

## 加新工具的步驟

1. 在 `backend/tools/` 下建或找對應的 `.py`，寫函式，回傳 `str`
2. 在 `backend/agent/tools.py` 加 import
3. 在 `TOOL_SCHEMAS` 加一筆 OpenAI function calling 格式的 schema
4. 在 `TOOL_HANDLERS` 加 `"函式名": 函式` 的對應
5. `AgentSession` 不加公車專屬分支；若需輸入預取，從入口注入 `input_enricher`

## 已知技術債

- **ebus 後端介面不是本專題控制的公開契約**：目前雲林站牌資料覆蓋足夠，provider 存取集中在 `backend/tools/yunlin_ebus.py`。若 endpoint 或 payload 改版，先修這層，不把格式假設散到 agent loop。
- **Compact 後的完整內容暫無 retrieval tool**：`AgentSession` 會把 transcript 與長 tool result 保存到 `.agent_state/`，active context 只保留摘要、路徑與預覽。若後續讓 agent 主動回讀壓縮資料，需要明確的 retrieval tool 與資料保留策略。
- **Chat session 存活在 api/chat.py process 記憶體**：`_chat_sessions` dict 不跨 worker、不跨重啟存活；session TTL 30 分鐘靠 `_purge_expired_sessions()` 懶清，適合單機 kiosk，若要 scale out 需改用外部 store。

## 已知 Gotcha

- **route lookup 是 stop-scoped**：`get_arrivals_here` 與 `get_route_stops` 先從 `KIOSK_STOP` 的 `/api/stop/route` 找 route id。沒停靠本站的 route 不會用全縣 route 清單硬查，避免同名 route 歧義
- **站名縮寫要人工處理**：`"雲科大" in "雲林科技大學"` 是 `False`（非連續子字串）。縮寫對照表在 `backend/tools/kiosk_bus.py` 的 `_ALIASES`
- **tool_call_id 配對**：截斷 messages 必須以「輪」為單位，不能直接 `messages[-N:]`，否則 tool_call_id 沒有對應的 tool result，API 報錯
- **tool round limit 也要保配對**：達到上限時不可先把新的 assistant `tool_calls` append 進 history 再跳出，否則下一輪送 API 會缺對應 tool result
- **`.agent_state/` 是 runtime state**：transcript 與完整長 tool result 會寫在這裡，已由 `.gitignore` 排除；測試若要檢查內容應注入 `ContextStore(tmp_path)`
- **Telemetry 預設不外送內容**：harness 的 OTLP spans/metrics 只標 model、tool name、outcome、error type 與 latency，不把 user input、prompt、tool result 放進 attributes；若要開內容層級觀測，先定資料保留與遮罩策略
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
