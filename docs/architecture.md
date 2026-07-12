# 架構與目錄

本專案以固定站牌 Kiosk 為場域。後端提供即時離站決策、聊天 tool facade、路線規劃 API、ASR/TTS proxy；前端提供 Kiosk dashboard、PIP 數位站務員與地圖路線規劃。

## 核心邊界

- `AgentSession` 是 harness orchestration，不放公車領域邏輯。
- `IntentRouter` 用 Python regex 決定意圖與工具派送；LLM 只負責 UNKNOWN 的 phrasing。`ConvState` 追蹤對話狀態，不靠 LLM 從 messages 推斷。
- 公車資料來源集中在 provider，分類與決策集中在 service，Agent 看到的是 tool facade 回傳的字串。
- 路線規劃不是聊天文字 tool；前端確認目的地座標後呼叫 `POST /api/route-plans`。
- Context 以輪為單位硬上限（`MAX_EXCHANGES=5`）加 token budget trim；過長 tool result 直接截斷成預覽（不另外保存完整內容），避免單則訊息吃光 budget。
- 回覆是串流的：`AgentSession.respond_stream()` 逐句 yield（`respond()` = 串接所有 chunk，單一路徑）。首輪 forced tool call 不串流；auto 輪的 content delta 經 `pipeline/normalize.StreamNormalizer`（增量 think 剝除 + 逐句 s2twp）輸出。不變式：完整回覆 == 所有 chunk 串接；串流輪的歷史 assistant content == 實際播出文字。
- Telemetry 只記 model、tool name、outcome、error type、latency 等 metadata，預設不記內容。

## 後端

```text
backend/
  config.py        # Settings（lru_cache singleton）、make_agent_session、_make_llm_client
  api/             # FastAPI app 與 HTTP endpoints
  agent/           # Agent harness、LLM client、tool dispatch、prompt、context、telemetry
  voice/           # Pipecat WebRTC 語音全雙工 pipeline（VAD, STT, TTS, Agent Processor）
  pipeline/        # Mandarin -> HanloFlow -> Taibun 等文字處理 pipeline
  providers/       # 外部資料來源 adapter：ebus、TDX bus、OTP、TDX Moovo、HybridBusProvider
  services/        # 領域模型、分類、決策、provider facade
  tools/           # Agent 可見的 str facade
  scripts/         # GTFS / stop metadata 更新流程
  otp/             # 本機 OTP build input / graph；git 只保留資料夾與說明
  tests/
```

### API

- `api/__init__.py`：FastAPI app、CORS、router include、telemetry setup。
- `api/admin.py`：`/api/admin/kiosk`（GET/PUT）與 `/api/admin/stops`（GET）；供後台 UI 讀寫 runtime 站牌設定與站牌目錄。
- `api/chat.py`：`/api/chat/*`，SQLite-backed `ChatSessionStore`。`respond_in_session_stream` 為唯一實作路徑（voice 與 SSE 共用）；`POST /api/chat/sessions/{id}/messages/stream` 以 SSE 推 `{delta}`/`{done}`/`{error}` 事件；非串流 endpoint 已移除。
- `api/departures.py`：`/api/departures/here` 與路線詳情；`GET /api/departures/stream` SSE 推播——ETA warmup loop（25 s）每次刷新 cache 後 `notify_snapshot_refreshed()` 喚醒連線推最新 snapshot，40 s fallback 自刷新兜底。
- `api/route_plans.py`：`/api/route-plans` 與 `/api/kiosk`（含 direction）。
- `api/moovo.py`：`/api/moovo/*`。
- `api/asr.py`：Breeze ASR proxy（提供給文字模式與語音模組共用的辨識邏輯）。
- `api/tts.py`：HanloFlow -> Taibun -> Piper TTS proxy（REST 模式 fallback）。
- `api/voice.py`：`/api/voice/offer`，處理 WebRTC SDP 交換並在背景啟動語音 pipeline，支援 session_id 綁定。

### Voice Pipeline (WebRTC)

- `voice/pipeline.py`：Pipecat 語音管線組裝（SmallWebRTCTransport、VAD、中斷處理）與連線生命週期管理。
- `voice/stt_breeze.py`：繼承 Pipecat `STTService`，搭配 VAD 收集完整語句後呼叫 ASR 轉文字。
- `voice/agent_processor.py`：將原本的 `AgentSession` 封裝為 Pipecat 的 `FrameProcessor`，介接文字與語音的資料流。消費 `respond_in_session_stream` 逐 chunk 推 `TextFrame`——Pipecat TTS 句子聚合器在 LLM 還在生成時就開始逐句合成，首音延遲不再等完整回覆。
- `voice/tts_taigi.py`：繼承 Pipecat `TTSService`，封裝原有的文字前處理與 Piper TTS，輸出 PCM 音訊流供 WebRTC 播放。

### Agent

- `agent/session.py`：messages、router gate、tool-call loop、context recovery。
- `agent/router.py`：`IntentRouter`、`ConvState`、`Decision` — regex-based intent classification。
- `agent/llm_client.py`：OpenAI-compatible LLM call、retry/backoff、context overflow。
- `agent/tool_dispatch.py`：tool call parse 與 dispatch。
- `agent/tools.py`：`TOOL_SCHEMAS` 與 `TOOL_HANDLERS`。
- `agent/context.py`：token budget、exchange-count cap、長 tool result 截斷。
- `telemetry.py`（backend 根）：OpenTelemetry spans / metrics；cross-cutting infra，與 `config.py` 同層，任何層都可引用。

### 領域層

- `providers/bus.py`：`BusProvider` Protocol（TDX-native flat dict schema；`sub_route_name`/`direction`/`stop_status`/`estimate_seconds` 等欄位）。
- `providers/http.py`：process-wide 共用 `httpx.AsyncClient`（連線池重用）；TTS/ASR/OTP/TDX/ebus 都透過它發請求，各呼叫點自帶 per-request timeout，app shutdown 時由 lifespan 關閉。
- `providers/ebus.py`：ebus.yunlin.gov.tw `BusProvider` 實作。提供 ComeTime（scheduled_time）與即時 estimate_seconds。無觀測到的 rate limit，所有路線並行抓取。route estimate 結果快取 30 s。
- `providers/tdx_bus.py`：TDX `BusProvider` 實作。同時查 `City/YunlinCounty`（市區公車）與 `InterCity`（公路客運）兩個 endpoint 並合併。OAuth2 token 自動快取。route_id 以 SubRouteName string 為主鍵。
- `providers/hybrid.py`：`HybridBusProvider`，線上唯一 `BusProvider` runtime 實例。路線目錄（`load_route_info`、`fetch_routes_at_stop`）由 TDX 提供；ETA（`fetch_eta_at_stop`、`fetch_route_estimate`）由 ebus 主力，ebus 空值時才 fallback 至 TDX intercity。
- `providers/otp.py`：OpenTripPlanner GraphQL provider。
- `providers/moovo.py`：TDX bike provider。
- `services/kiosk_config.py`：Runtime kiosk 設定 singleton（stop_name、direction、lat/lon）；持久化至 `.agent_state/kiosk_config.json`，預設雲林科技大學／回程。所有需要站牌資訊的模組從此讀取，不用 env var。
- `services/departures/`：離站決策唯一分類來源，支援 provider override。方向過濾分兩層：admin 設定「去程」或「回程」時直接照設定過濾（不做 auto-detect）；設定「去回程都有」（go_back=None）時啟動 `_is_terminal_direction()` 自動過濾「本站是該方向終點（即抵達非出發）」的方向，循環路線（go_dest == back_dest == 本站）不過濾。`_classify_stop` 讀 TDX `stop_status` / `estimate_seconds`，回傳 `StopClassification` dataclass，所有 render 函式共用同一分類規則。方向編碼 0=去程、1=回程（TDX Direction）。`route_id` 全層為 str（SubRouteName）。
- `services/route_plans.py`：OTP 路線規劃 facade、Kiosk 起點、雲林邊界、view model。
- `services/moovo.py`：公共自行車站 dataclass、解析、cache、距離查詢。
- `services/stop_catalog.py`：TDX / GTFS 更新流程產生的雲林 stop index。
- `services/yunlin_boundary.py`：雲林縣 GeoJSON point-in-polygon。
- `tools/kiosk_bus.py`：Agent str facade、站名縮寫。

## 前端

```text
frontend/
  public/avatar.png
  src/App.vue
  src/features/departures/
  src/features/route-planner/
  src/features/agent-chat/
  src/components/ui/
```

- `App.vue`：Kiosk shell，控制首頁與路線規劃 view。
- `features/departures/`：離站決策首頁、路線詳情、route colors、顯示狀態。資料以 `EventSource`（`/api/departures/stream`）為主，斷線時自動降級為輪詢。
- `features/route-planner/`：MapCN destination picker、路線規劃 request、指定時間 wheel；地圖顯示當前站牌名稱與方向。
- `features/admin/`：後台站牌切換 UI（`/admin`）；地圖搜尋選站、方向設定、即時套用；無密碼保護，設定立即生效。
- `features/agent-chat/`：PIP 對話 session 管理。整合 WebRTC 語音串流、打字動畫、與共用對話上下文 (Shared Session)，並保留 REST fallback 機制。
- `components/ui/`：shadcn-vue 與 MapCN Vue copy-paste UI components。

## 已知技術債

- TDX API 與 ebus API 都是外部契約；TDX 欄位或 endpoint 改版修 `providers/tdx_bus.py`，ebus 改版修 `providers/ebus.py`，路由邏輯改版修 `providers/hybrid.py`。
- Chat session 持久化在 `.agent_state/sessions.db`，目前仍綁單機檔案；scale out 需改外部 KV / Redis。
- Backend runtime 採 async 單一路徑；HTTP-facing providers、services、AgentSession tool dispatch 與 LLM client 都是 async。GTFS 更新腳本可用同步 requests，不屬於線上 API 路徑。
