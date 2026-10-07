# 架構參考（逐模組細節）

> 想先看全貌請讀 [architecture.md](architecture.md)。本檔是查表用：改某個模組前，來這裡找它的邊界與地雷。

## 核心邊界

- `AgentSession` 是 harness orchestration，不放公車領域邏輯。
- `IntentRouter` 用 Python regex 處理固定回覆；其餘請求交由 LLM 判斷並派送工具。`ConvState` 追蹤對話狀態，不靠 LLM 從 messages 推斷。
- 公車資料來源集中在 provider，分類與決策集中在 service，Agent 看到的是 tool facade 回傳的字串。
- 路線規劃不是聊天文字 tool；前端確認目的地座標後呼叫 `POST /api/route-plans`。
- Context 以輪為單位硬上限（`MAX_EXCHANGES=5`）加 token budget trim；過長 tool result 直接截斷成預覽（不另外保存完整內容），避免單則訊息吃光 budget。
- 回覆是串流的：`AgentSession.respond_stream()` 逐句 yield（`respond()` = 串接所有 chunk，單一路徑）。首輪 forced tool call 不串流；auto 輪的 content delta 經 `pipeline/normalize.StreamNormalizer`（增量 think 剝除 + 逐句 s2twp）輸出。不變式：完整回覆 == 所有 chunk 串接；串流輪的歷史 assistant content == 實際播出文字。
- Telemetry 記錄 spans / metrics / logs，文字內容預設收集並截斷；可用 `TELEMETRY_CAPTURE_CONTENT=false` 關閉內容層級觀測。

## 快取

到站資料都在 `providers/tdx_bus.py` 快取。上游失敗、被限流或等超過 3 秒預算時，會拿舊資料頂著。

| 資料 | 新鮮多久 | 上游失敗時，舊資料最多再用多久 | 存的是 |
|---|---|---|---|
| 本站到站（`fetch_eta_at_stop`） | 60 秒 | 再 300 秒 | TDX 的**相對秒數** |
| 單一路線預估（`fetch_route_estimate`） | 30 秒 | 再 300 秒 | TDX 的**相對秒數** |
| 路線站序（StopOfRoute） | 10 分鐘；上次只抓到一半則 60 秒 | 重抓失敗就保留上一份快照 | 靜態站序 |
| TDX token | 依 `expires_in` 提前 60 秒換新；沒給就當 1 小時 | — | OAuth token |
| 公共自行車站（`services/bike.py`） | 60 秒（`BIKE_CACHE_TTL_SECONDS`） | — | 站點與可借車數 |

另外，背景 warmup 每 25 秒檢查一次本站到站；因為快取 60 秒才過期，實際約每 75 秒才重抓上游一次。每次檢查後都經 SSE 推最新狀態給首頁。

**已知問題**：到站存的是抓取當下的相對秒數，拿舊資料時不會扣掉已經過去的時間。本站到站的資料最舊可到 60 + 300 = 360 秒，所以被限流時「約 N 分鐘後」最多可能差約 6 分鐘。

## 後端

```text
backend/
  config.py        # Settings（lru_cache singleton）、make_agent_session、_make_llm_client
  async_lifecycle.py  # 跨層共用的 cancellation-safe async 資源生命週期原語
  api/             # FastAPI app 與 HTTP endpoints
  agent/           # Agent harness、LLM client、tool dispatch、prompt、context、telemetry
  voice/           # Pipecat WebRTC 語音全雙工 pipeline（VAD, STT, TTS, Agent Processor）
  pipeline/        # Mandarin -> HanloFlow -> Taibun 等文字處理 pipeline
  providers/       # 外部資料來源 adapter：provider-neutral bus、bike、OTP
  services/        # 領域模型、分類、決策、provider facade
  tools/           # Agent 可見的 str facade
  scripts/         # GTFS / stop metadata 更新流程
  otp/             # 本機 OTP build input / graph；git 只保留資料夾與說明
  e2e_test.py      # 用真的 LLM 與工具跑一段腳本對話，印出來人工看（不是 pytest）
  tests/
```

### 資源生命週期

`async_lifecycle.py` 是 backend 根層的 cross-cutting infra，任何層都可引用。它取代「fire-and-forget task + best-effort close」這種容易洩漏或重複關閉的寫法：建立 task、等待、取消、關閉都要經過一個 owner，關閉失敗的資源會留著重試，不會只剩一行 log。各原語的語意寫在該檔的 docstring。

用到它的地方例如：`providers/http.py`、`config.py`（LLM client）、`api/chat.py`（store lease）、`api/voice.py`、`voice/pipeline.py`、`voice/agent_processor.py`、`voice/webrtc.py`。

### API

- `api/__init__.py`：FastAPI app、CORS、router include、telemetry setup，以及全域 request body middleware。POST/PUT/PATCH 預設上限 64 KB；`/api/asr` 含 multipart overhead 上限 26 MB，chunked body 也在串流接收時累計並回 413。
- `api/admin.py`：`/api/admin/kiosk`（GET/PUT）與 `/api/admin/stops`（GET）；寫入採 `ADMIN_TOKEN` fail-closed 驗證，且只接受站名與方向。座標由後端 stop catalog 的主要群集計算，不能由前端覆寫。
- `api/chat.py`：`/api/chat/*`，SQLite-backed `ChatSessionStore`。`respond_in_session_stream` 為唯一實作路徑（voice 與 SSE 共用）；`chat_store_operation()` 是 store 的公開存取點：它取一個 lease 綁住當前 store generation，voice（`api/voice.py`、`voice/agent_processor.py`）與 SSE 都經它取得同一個 store，不跨層 import 底線符號；lifespan 以 `startup_store()`／`close_store()` 管理 generation，關閉會等所有 lease 交還。`PUT /api/chat/sessions/{id}` 以 client-owned UUID 冪等建立 session；`POST /api/chat/sessions/{id}/messages/stream` 以 SSE 推 `{delta}`/`{done}`/`{error}` 事件。
- `api/departures.py`：`/api/departures/here` 與路線詳情；`GET /api/departures/stream` SSE 推播，warmup 刷新快取後喚醒連線推最新 snapshot，40 秒沒動靜就自己刷新兜底。兩個 GET 端點刻意不掛 RateLimit：它們是 Kiosk 自己的高頻主路徑，讀的是 provider 快取（見「快取」），不會每次都打上游。
- `api/route_plans.py`：`/api/route-plans` 與 `/api/kiosk`（含 direction）。
- `api/bike.py`：`/api/bike/*`。
- `api/asr.py`：`/api/asr` proxy，config 讀取與 upstream 呼叫委派給 `providers/asr.py`（文字模式與語音模組共用）；音檔以 1 MB chunk 讀取，實際檔案內容上限 25 MB。
- `api/tts.py`：`/api/tts`，呼叫 `services/taigi_tts.py` 的共用 pipeline 後轉成 WAV `Response`。Tailo 最多 64 段、每段最多 500 字元、同時最多 4 個 upstream request；單請求 15 秒、整次合成 45 秒。
- `api/voice.py`：`/api/voice/offer`，處理 WebRTC SDP 交換並在背景啟動語音 pipeline；`session_id` 是必填且必須先由 chat PUT 建立。session 不存在或過期時回 404，其他啟動失敗回 500，兩者都會關閉該 peer connection——pipecat 會吞掉 callback 例外並照樣發 SDP answer，不主動關就會留下沒有 pipeline 的孤兒 pc。
- `api/client_events.py`：`POST /api/client-events`，收前端錯誤回報，轉成 `log_diagnostic(scope="client")`。
- `api/sse.py`：共用 SSE 樣板（`SSE_HEADERS`、`sse_event()`），供 `api/chat.py` 與 `api/departures.py` 共用。

### Voice Pipeline (WebRTC)

- `voice/pipeline.py`：Pipecat 語音管線組裝（SmallWebRTCTransport、VAD、中斷處理）與連線生命週期管理。`SubtitleSyncProcessor` 掛在 `transport.output()` 之後，攔自訂 `SubtitleFrame`（`tts_taigi.run_tts` 在音訊 frames **之前** yield，帶該段精確音訊時長；不可繼承 TTSTextFrame、不可設 pts）——事件到達 ≈ 段起播，前端在 durationMs 內線性逐字揭示，全文 `agent_reply` 由 `bot_silent` 收尾補全。注意：pipecat 預設段尾 TTSTextFrame 保持存在（`push_text_frames=False` 會觸發 WordCompletionTracker 段尾補發全文，是坑），無人消費即可。
- `voice/stt_breeze.py`：繼承 Pipecat `SegmentedSTTService`，搭配 VAD 收集完整語句後呼叫 `providers/asr.py` 轉文字（與 `api/asr.py` 共用同一 provider，不 import `api/`）。
- `voice/agent_processor.py`：將 `AgentSession` 封裝為 Pipecat 的 `FrameProcessor`。消費 `respond_in_session_stream` 逐 chunk 推 `TextFrame`，TTS 句子聚合器在 LLM 還在生成時就開始逐句合成。`_open_stream()` 處理 live voice 期間的 session 過期：首次 LookupError 以原本 client-owned ID 重建並重試一次；若該 ID 已被明確 DELETE，便不復活。
- `voice/tts_taigi.py`：繼承 Pipecat `TTSService`，呼叫 `services/taigi_tts.py` 取得 pre-decode 結果後自行解成 PCM 音訊流供 WebRTC 播放。
- `voice/webrtc.py`：包住 Pipecat 的 `SmallWebRTCConnection`。pipecat 會 detach event handler 且留下未 join 的內部 task，這層用 `async_lifecycle` 重做生命週期邊界，保留原本的 media 行為。

### Agent

- `agent/session.py`：messages、router gate、tool-call loop、context recovery。
- `agent/router.py`：`IntentRouter`、`ConvState`、`Decision` — regex-based intent classification。
- `agent/llm_client.py`：OpenAI-compatible LLM call、retry/backoff、context overflow。
- `agent/tool_dispatch.py`：tool call parse 與 dispatch；每次工具呼叫給 3 秒上游預算。
- `agent/tools.py`：`TOOL_SCHEMAS` 與 `TOOL_HANDLERS`。
- `agent/context.py`：token budget、exchange-count cap、長 tool result 截斷。
- `telemetry.py`（backend 根）：OpenTelemetry spans / metrics；cross-cutting infra，與 `config.py` 同層，任何層都可引用。

### 領域層

- `providers/bus.py`：provider-neutral `BusProvider` Protocol 與 `RouteInfo`、`RouteAtStop`、`StopArrival`、`RouteStopEstimate` model。`RouteInfo` 除去回終點外也帶靜態站序（`outbound_stops` / `inbound_stops`，依站序排列）。契約裡沒有任何上游欄位名或 status code，adapter 必須回傳完整 typed row。
- `providers/http.py`：process-wide 共用 `httpx.AsyncClient`（連線池重用）；TTS/ASR/OTP/TDX 都透過它發請求，各呼叫點自帶 per-request timeout，app shutdown 時由 lifespan 關閉。
- `providers/tdx_bus.py`：TDX 的 provider-neutral adapter；整合 City/InterCity endpoint，OAuth2、快取（見「快取」）與 retry 都封裝在 adapter 內。互動呼叫被 429 時不等 Retry-After，直接讓快取供舊資料。
- `providers/tdx_rate_limit.py`：每把 TDX 金鑰一個程序內共用的滑動視窗限速器（`TDX_RATE_LIMIT`，預設 5/min）。公車與單車 adapter 每個請求都先取額度；背景呼叫只能用約 60%，其餘留給有預算的互動呼叫；429 依 Retry-After 暫停整把金鑰。所有等待都在這裡，provider 不自己 sleep。
- `upstream_deadline.py`：每次請求的上游時間預算（ContextVar）。工具呼叫與 Kiosk 畫面的到站讀取各給 3 秒；`providers/ttl_cache.py` 與 TDX adapter 依剩餘預算決定是否等待。
- `services/departures/provider.py`：composition root，固定組裝 `TdxBusProvider`；換資料源就是在這裡換接線。`set_provider()` / `provider_override()` / `reset_provider()` 供測試替換。
- `providers/otp.py`：OpenTripPlanner GraphQL provider。
- `providers/bike.py`：provider-neutral bike contract 與 `BikeStation` model；上游沒提供的欄位一律留 `None`，不得以 0 代替（0 在前端等同「沒車可借」）。
- `providers/tdx_bike.py`：TDX Bike adapter；OAuth、HTTP、native payload normalization 都封裝在 adapter 內。
- `providers/moovo_website.py`：MOOVO 官方城市地圖 adapter；只輸出官網能提供的站點座標與可借車數。
- `providers/fallback_bike.py`：provider-neutral bike fallback。provider 拋錯或回空清單都視為不可用，往下一個來源退，只有非空快照才算命中。
- `services/bike_provider.py`：bike provider composition root，可透過 `BIKE_PROVIDER_ORDER`（預設 `tdx,moovo_web`）或 `configure_providers()` 切換與加入 provider。
- `providers/asr.py`：ASR upstream provider（config 讀取 + multipart 上傳），供 `api/asr.py` 與 `voice/stt_breeze.py` 共用。
- `services/taigi_tts.py`：TTS config、Tailo 切段、`synthesize_segments` 有界並發派送；`prepare_tailo()` 收斂 normalize → text-process → split 的共用序列，`api/tts.py` 與 `voice/tts_taigi.py` 各自接手錯誤轉換與音訊解碼（WAV vs PCM）。
- `services/kiosk_config.py`：Runtime kiosk 設定 singleton（stop_name、direction、lat/lon）；先原子落盤再發布記憶體狀態，並用 mtime 觀察其他 worker 的更新。持久化至 `.agent_state/kiosk_config.json`，預設雲林科技大學／回程。
- `services/departures/`：離站決策唯一分類來源，只讀 provider-neutral `StopStatus`、`eta_seconds` 與 `RouteInfo`。狀態門檻在 `classification.py`（≤3 分即將到站、≤20 分可以等）。
  目的地查詢（`render_arrivals_to_destination`）先用 `RouteInfo` 靜態站序判斷哪些路線在本站之後會到目的地（不含上車點本身，循環路線回到本站的那一站仍算），只對命中的路線抓即時 route estimate；查無與聽錯救援的候選也全由靜態站序產生，不打上游。
- `services/route_plans.py`：OTP 路線規劃 facade、Kiosk 起點、雲林邊界、view model。
- `services/bike.py`：公共自行車 normalized station cache、provider switching、距離查詢；服務層與 `/api/bike/*` 一律用中立的 bike 命名。
- `services/stop_catalog.py`：TDX / GTFS 更新流程產生的雲林 stop index。
- `services/yunlin_boundary.py`：雲林縣 GeoJSON point-in-polygon。
- `tools/kiosk_bus.py`：Agent str facade，解析 kiosk 範圍（stop/direction）後轉呼叫 `services.departures`。

## 前端

```text
frontend/
  src/App.vue
  src/features/departures/      # 離站首頁、路線詳情
  src/features/route-planner/   # 地圖選點與路線規劃
  src/features/agent-chat/      # 小芸：對話、WebRTC 語音、Live2D
  src/features/admin/           # /admin 後台
  src/components/ui/            # shadcn-vue 與地圖元件（maplibre-gl）
  src/lib/                      # 共用工具：資源生命週期、路線色
  tests/                        # vitest（pnpm test）
```

- `App.vue`：Kiosk shell，控制首頁與路線規劃 view。
- `features/departures/`：資料以 `EventSource`（`/api/departures/stream`）為主，斷線時自動降級為輪詢。
- `features/route-planner/`：destination picker、路線規劃 request、指定時間 wheel、公共自行車圖層；地圖顯示當前站牌名稱與方向。
- `features/admin/`：token 僅保存於目前瀏覽器 `sessionStorage`。地圖搜尋選站與方向後，由後端驗證站名並套用 canonical 座標。
- `features/agent-chat/`：PIP 對話 session 管理，整合 WebRTC 語音串流、字幕、共用對話上下文，並保留 REST fallback。Live2D 表情由 `live2d/expressionStates.ts` 依對話狀態切換。
- `lib/resource-owner.ts` 等 `*-owner.ts`、`*-lifecycle.ts`：後端 `async_lifecycle.py` 的前端對應版，管住地圖、計時器、WebRTC、音訊這些要確實釋放的資源。
- `lib/route-colors.ts`、`lib/useRouteColors.ts`：路線色。從 24 色色池裡，讓同一畫面上的路線顏色盡量拉開；不是全雲林唯一色，路線一多仍會重複。細節見檔內註解。

## 已知技術債

- 快取的到站時間不會隨時間遞減（見「快取」）。
- TDX 是唯一公車資料來源：沒有 `scheduled_time`（未發車的預計發車時刻），畫面上「尚未發車」不會附時刻；TDX 掛掉超過舊資料寬限（約 5 分鐘）就只能回「查詢失敗」。
- Chat session 持久化在 `.agent_state/sessions.db`，綁單機檔案；scale out 需改外部 KV / Redis。
- API rate limit 與 TDX 限速器都是單一程序內的；多 worker 或多機部署要在 gateway 另設全域限流。
- Backend runtime 採 async 單一路徑；HTTP-facing providers、services、AgentSession tool dispatch 與 LLM client 都是 async。GTFS 更新腳本可用同步 requests，不屬於線上 API 路徑。
