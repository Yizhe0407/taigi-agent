# 架構與目錄

本專案以固定站牌 Kiosk 為場域。後端提供即時離站決策、聊天 tool facade、路線規劃 API、ASR/TTS proxy；前端提供 Kiosk dashboard、PIP 數位站務員與地圖路線規劃。

## 核心邊界

- `AgentSession` 是 harness orchestration，不放公車領域邏輯。
- `IntentRouter` 用 Python regex 處理固定回覆；其餘請求交由 LLM 判斷並派送工具。`ConvState` 追蹤對話狀態，不靠 LLM 從 messages 推斷。
- 公車資料來源集中在 provider，分類與決策集中在 service，Agent 看到的是 tool facade 回傳的字串。
- 路線規劃不是聊天文字 tool；前端確認目的地座標後呼叫 `POST /api/route-plans`。
- Context 以輪為單位硬上限（`MAX_EXCHANGES=5`）加 token budget trim；過長 tool result 直接截斷成預覽（不另外保存完整內容），避免單則訊息吃光 budget。
- 回覆是串流的：`AgentSession.respond_stream()` 逐句 yield（`respond()` = 串接所有 chunk，單一路徑）。首輪 forced tool call 不串流；auto 輪的 content delta 經 `pipeline/normalize.StreamNormalizer`（增量 think 剝除 + 逐句 s2twp）輸出。不變式：完整回覆 == 所有 chunk 串接；串流輪的歷史 assistant content == 實際播出文字。
- Telemetry 記錄 spans / metrics / logs，文字內容預設收集並截斷；可用 `TELEMETRY_CAPTURE_CONTENT=false` 關閉內容層級觀測。

## 後端

```text
backend/
  config.py        # Settings（lru_cache singleton）、make_agent_session、_make_llm_client
  async_lifecycle.py  # 跨層共用的 cancellation-safe async 資源生命週期原語
  api/             # FastAPI app 與 HTTP endpoints
  agent/           # Agent harness、LLM client、tool dispatch、prompt、context、telemetry
  voice/           # Pipecat WebRTC 語音全雙工 pipeline（VAD, STT, TTS, Agent Processor）
  pipeline/        # Mandarin -> HanloFlow -> Taibun 等文字處理 pipeline
  providers/       # 外部資料來源 adapter：provider-neutral bus、OTP、TDX Moovo
  services/        # 領域模型、分類、決策、provider facade
  tools/           # Agent 可見的 str facade
  scripts/         # GTFS / stop metadata 更新流程
  otp/             # 本機 OTP build input / graph；git 只保留資料夾與說明
  tests/
```

### 資源生命週期 (async_lifecycle.py)

`async_lifecycle.py` 是 backend 根層的 cross-cutting infra（與 `telemetry.py` 同層，任何層都可引用），提供 cancellation-safe 的 async 資源生命週期原語，取代「fire-and-forget task + best-effort close」這種容易洩漏或重複關閉的寫法：

- `create_lifecycle_task()`：建立 task 但不讓 Python 3.12 eager task factory 在 owner 把它登記進權威 slot/set 之前就同步執行，避免 close/replace 邏輯看不到已啟動的資源。
- `join_task()` / `cancel_and_join_task()`：用 `asyncio.shield` 等待實體 task，等待者被 cancel 不會連帶 cancel 資源本身；多個 caller 收斂到同一次實體 teardown 而不是各自重複 cancel。
- `run_in_thread()`：把 `asyncio.to_thread` 包進一個 lifecycle task 再 join，讓「等待者被 cancel」不再等於「執行緒被丟下不管」。
- `ReclaimingAsyncLock`：只在有 holder/waiter 時才存在的 `asyncio.Lock`，避免長壽命 module-level 物件跨 event loop generation（例如測試之間）持有一把綁死舊 loop 的鎖。
- `AsyncResourceOwner[T]`：一組資源的權威登記表，close 失敗時資源不會變成「只留 log 的洩漏」，而是留著讓 `retry_failed()` 或 `aclose()` 重試；`begin_acquisition()`/`finish_acquisition()`/`abort_acquisition()` 專門處理建構子中間會跨 `await` 的情況，terminal `aclose()` 會先關閉 acquisition gate、等所有已在途的建構完成或中止，才 drain 現有資源，避免關閉後還有殘留 continuation 復活資源。

實際套用點：`providers/http.py`（process-wide `httpx.AsyncClient`）、`config.py`（每個 LLM client 各自的 HTTP owner，串流失敗時下一輪呼叫前 `retry_failed()`）、`api/chat.py`（`chat_store_operation()` 的 store generation lease）、`api/voice.py`（WebRTC connection 與 voice pipeline runtime 兩個 owner）、`voice/pipeline.py`、`voice/agent_processor.py`（agent response streams owner）。`voice/webrtc.py` 是在此之上包的一層：因為 pipecat 的 `SmallWebRTCConnection` 會 detach event handler 且留下未 join 的內部 task，這層用 `async_lifecycle` 的原語重做這些生命週期邊界，同時保留原本的 media 行為。

### API

- `api/__init__.py`：FastAPI app、CORS、router include、telemetry setup，以及全域 request body middleware。POST/PUT/PATCH 預設上限 64 KB；`/api/asr` 含 multipart overhead 上限 26 MB，chunked body 也在串流接收時累計並回 413。
- `api/admin.py`：`/api/admin/kiosk`（GET/PUT）與 `/api/admin/stops`（GET）；寫入採 `ADMIN_TOKEN` fail-closed 驗證，且只接受站名與方向。座標由後端 stop catalog 的主要群集計算，不能由前端覆寫。
- `api/chat.py`：`/api/chat/*`，SQLite-backed `ChatSessionStore`。`respond_in_session_stream` 為唯一實作路徑（voice 與 SSE 共用）；`chat_store_operation()` 是 store 的公開存取點：它取一個 lease 綁住當前 store generation，voice（`api/voice.py`、`voice/agent_processor.py`）與 SSE 都經它取得同一個 store，不跨層 import 底線符號；lifespan 以 `startup_store()`／`close_store()` 管理 generation，關閉會等所有 lease 交還。`PUT /api/chat/sessions/{id}` 以 client-owned UUID 冪等建立 session；`POST /api/chat/sessions/{id}/messages/stream` 以 SSE 推 `{delta}`/`{done}`/`{error}` 事件；舊 server-generated create 與非串流 endpoint 已移除。
- `api/departures.py`：`/api/departures/here` 與路線詳情；`GET /api/departures/stream` SSE 推播——ETA warmup loop（25 s）每次刷新 cache 後 `notify_snapshot_refreshed()` 喚醒連線推最新 snapshot，40 s fallback 自刷新兜底。兩個 GET 端點刻意不掛 RateLimit：是 kiosk 自家高頻主路徑，且 `services.departures` 對底層 snapshot 已有 25 s cache。
- `api/route_plans.py`：`/api/route-plans` 與 `/api/kiosk`（含 direction）。
- `api/moovo.py`：`/api/moovo/*`。
- `api/asr.py`：`/api/asr` proxy，config 讀取與 upstream 呼叫委派給 `providers/asr.py`（文字模式與語音模組共用）；音檔以 1 MB chunk 讀取，實際檔案內容上限 25 MB。
- `api/tts.py`：`/api/tts`，呼叫 `services/taigi_tts.py` 的共用 pipeline（見下）後轉成 WAV `Response`。Tailo 最多 64 段、每段最多 500 字元、同時最多 4 個 upstream request；單請求 15 秒、整次合成 45 秒。
- `api/voice.py`：`/api/voice/offer`，處理 WebRTC SDP 交換並在背景啟動語音 pipeline；`session_id` 是必填且必須先由 chat PUT 建立。session 不存在或過期時回 404，其他啟動失敗回 500，兩者都會關閉該 peer connection——pipecat 會吞掉 callback 例外並照樣發 SDP answer，不主動關就會留下沒有 pipeline 的孤兒 pc。
- `api/sse.py`：共用 SSE 樣板（`SSE_HEADERS`、`sse_event()`），供 `api/chat.py` 與 `api/departures.py` 共用。

### Voice Pipeline (WebRTC)

- `voice/pipeline.py`：Pipecat 語音管線組裝（SmallWebRTCTransport、VAD、中斷處理）與連線生命週期管理。`SubtitleSyncProcessor` 掛在 `transport.output()` 之後，攔自訂 `SubtitleFrame`（`tts_taigi.run_tts` 在音訊 frames **之前** yield，帶該段精確音訊時長；不可繼承 TTSTextFrame、不可設 pts）——事件到達 ≈ 段起播，前端在 durationMs 內線性逐字揭示，全文 `agent_reply` 由 `bot_silent` 收尾補全。注意：pipecat 預設段尾 TTSTextFrame 保持存在（`push_text_frames=False` 會觸發 WordCompletionTracker 段尾補發全文，是坑），無人消費即可。
- `voice/stt_breeze.py`：繼承 Pipecat `SegmentedSTTService`，搭配 VAD 收集完整語句後呼叫 `providers/asr.py` 轉文字（與 `api/asr.py` 共用同一 provider，不 import `api/`）。
- `voice/agent_processor.py`：將原本的 `AgentSession` 封裝為 Pipecat 的 `FrameProcessor`，介接文字與語音的資料流。消費 `respond_in_session_stream` 逐 chunk 推 `TextFrame`——Pipecat TTS 句子聚合器在 LLM 還在生成時就開始逐句合成，首音延遲不再等完整回覆。`_open_stream()` 把 live voice 期間的 TTL recovery 收斂成一個 helper：首次 LookupError 會以原本 client-owned ID 重建並重試一次；若該 ID 已被明確 DELETE tombstone，便停止 recovery，不會復活已結束的 session。
- `voice/tts_taigi.py`：繼承 Pipecat `TTSService`，呼叫 `services/taigi_tts.py` 的共用 pipeline 取得 pre-decode 結果後自行解成 PCM 音訊流供 WebRTC 播放（`api/tts.py` 解成 WAV `Response`）。
- `voice/webrtc.py`：專案自有的 WebRTC 生命週期 adapter，包住 Pipecat 的 `SmallWebRTCConnection`（詳見「資源生命週期」一節）。

### Agent

- `agent/session.py`：messages、router gate、tool-call loop、context recovery。
- `agent/router.py`：`IntentRouter`、`ConvState`、`Decision` — regex-based intent classification。
- `agent/llm_client.py`：OpenAI-compatible LLM call、retry/backoff、context overflow。
- `agent/tool_dispatch.py`：tool call parse 與 dispatch。
- `agent/tools.py`：`TOOL_SCHEMAS` 與 `TOOL_HANDLERS`。
- `agent/context.py`：token budget、exchange-count cap、長 tool result 截斷。
- `telemetry.py`（backend 根）：OpenTelemetry spans / metrics；cross-cutting infra，與 `config.py` 同層，任何層都可引用。

### 領域層

- `providers/bus.py`：provider-neutral `BusProvider` Protocol 與 `RouteInfo`、`RouteAtStop`、`StopArrival`、`RouteStopEstimate` model，以及 `BusProviderConfigError`。契約裡沒有任何上游欄位名或 status code，adapter 必須回傳完整 typed row（不再有 dict 相容層或 `as_*` coercion）。
- `providers/http.py`：process-wide 共用 `httpx.AsyncClient`（連線池重用）；TTS/ASR/OTP/TaiwanBus/TDX/ebus 都透過它發請求，各呼叫點自帶 per-request timeout，app shutdown 時由 lifespan 關閉。
- `providers/taiwan_bus.py`：TaiwanBus eBUS provider-neutral adapter；解析路線搜尋、route key 與即時資料，所有 native payload 在 adapter 內轉成 `providers.bus` model。
- `providers/ebus.py`：ebus.yunlin.gov.tw 的 provider-neutral adapter；保留 route index 與 upstream cache，所有 native payload 在 adapter 內轉成 `providers.bus` model。預設鏈不含它，用 `BUS_PROVIDER_ORDER` 選入即可啟用；`.agent_state/ebus-route-index.json` 路徑由 `EBUS_ROUTE_INDEX_PATH` 覆寫（schema v2 存 provider-neutral 欄位名）。
- `providers/tdx_bus.py`：TDX 的 provider-neutral adapter；整合 City/InterCity endpoint，OAuth2、TTL、LRU 與 retry 都封裝在 adapter 內。
- `providers/fallback.py`：provider-neutral `FallbackBusProvider`，接受任意長度的有序 provider 清單。只依賴 `BusProvider` Protocol，不知道 TaiwanBus、Ebus、TDX 或其他具體供應商。各操作的「這個來源沒東西」訊號不同：`fetch_routes_at_stop` 空清單代表不認得這站會續查下一個；ETA 與 route estimate 的 `None` 代表不可用、`[]` 是真答案並結束查詢；`load_route_info` 則沿鏈合併直到每條路線的去回終點都補齊（前面的來源優先）。結果以中立的 fallback metrics 記錄。
- `services/departures/provider.py`：composition root 與 registry。`BUS_PROVIDER_ORDER`（逗號分隔，預設 `taiwanbus,tdx`；內建名稱 `taiwanbus` / `tdx` / `ebus`）決定鏈的順序，`register_provider()` 可在組裝前加入新來源，`configure_providers()` / `provider_override()` / `reset_provider()` 供測試與 runtime 切換；其他層不需要知道具體供應商。
- `providers/otp.py`：OpenTripPlanner GraphQL provider。
- `providers/moovo.py`：TDX bike provider。
- `providers/asr.py`：ASR upstream provider（config 讀取 + multipart 上傳），供 `api/asr.py` 與 `voice/stt_breeze.py` 共用，兩邊都不再互相 import 私有符號。
- `services/taigi_tts.py`：TTS config、Tailo 切段、`synthesize_segments` 有界並發派送；`prepare_tailo()` 收斂 normalize 後→text-process→split 的共用序列（回傳解碼前的 hanlo/tailo/segments），`api/tts.py` 與 `voice/tts_taigi.py` 各自接手 `synthesize_segments` 的錯誤轉換與音訊解碼（WAV vs PCM）。`make_silence_pcm()` 是兩邊共用的靜音位元組運算。
- `services/kiosk_config.py`：Runtime kiosk 設定 singleton（stop_name、direction、lat/lon）；先原子落盤再發布記憶體狀態，並用 mtime 觀察其他 worker 的更新。持久化至 `.agent_state/kiosk_config.json`，預設雲林科技大學／回程。
- `services/departures/`：離站決策唯一分類來源，只讀 provider-neutral `StopStatus`、`eta_seconds` 與 `RouteInfo`；不依賴任何上游名稱或 status code。
- `services/route_plans.py`：OTP 路線規劃 facade、Kiosk 起點、雲林邊界、view model。
- `services/moovo.py`：公共自行車站 dataclass、解析、cache、距離查詢。
- `services/stop_catalog.py`：TDX / GTFS 更新流程產生的雲林 stop index。
- `services/yunlin_boundary.py`：雲林縣 GeoJSON point-in-polygon。
- `tools/kiosk_bus.py`：Agent str facade，解析 kiosk 範圍（stop/direction）後轉呼叫 `services.departures`。

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
- `features/admin/`：後台站牌切換 UI（`/admin`）；token 僅保存於目前瀏覽器 `sessionStorage`。地圖搜尋選站與方向後，由後端驗證站名並套用 canonical 座標。
- `features/agent-chat/`：PIP 對話 session 管理。整合 WebRTC 語音串流、打字動畫、與共用對話上下文 (Shared Session)，並保留 REST fallback 機制。
- `components/ui/`：shadcn-vue 與 MapCN Vue copy-paste UI components。
- `lib/resource-owner.ts`、`lib/async-release-owner.ts`、`lib/timer-owner.ts`、`lib/component-lifecycle.ts`、`lib/application-lifecycle.ts`：對應後端 `async_lifecycle.py` 的前端資源生命週期原語。`resource-owner.ts` 是核心（`ResourceOwner`：claim/acquire 一個資源、`close()` 是一次性的永久 gate、`AbortSignal` 供內部 async 操作接取消）；`timer-owner.ts` 把 `setTimeout`/`requestAnimationFrame` 包成受 owner 管理的 `TimerLease`；`async-release-owner.ts` 收斂多個並行 async release、可一起 `settle()`；`component-lifecycle.ts` 把 sync/async teardown 掛在 `onScopeDispose()`，供 Vue composable 註冊解構順序與失敗回報；`application-lifecycle.ts`（見 `main.ts`）用同一套 gate 包住整個 App 生命週期（mount、client event reporting）。實際套用點：`Map.vue`、`useWebRTC.ts`、`useTts.ts`、`officialCubismAvatar.ts` 等每個持有地圖/計時器/WebRTC/音訊資源的地方。

## 已知技術債

- TaiwanBus、TDX、ebus 三個 API 都是外部契約，各自的欄位或 endpoint 改版只修自己的 adapter（`providers/taiwan_bus.py`、`providers/tdx_bus.py`、`providers/ebus.py`）；鏈的順序改設定，fallback 語意改 `providers/fallback.py`。三個來源能填的欄位不同（TDX 沒有 `scheduled_time` 與 `vehicle_id`、只有 TDX 產得出 `NOT_STOPPING` / `NOT_OPERATING`、TaiwanBus 的方向是從站序分組推導而非上游宣告），所以切換來源時畫面上的「尚未發車」預計時刻、車號與方向標籤可能跟著變。
- Chat session 持久化在 `.agent_state/sessions.db`，目前仍綁單機檔案；scale out 需改外部 KV / Redis。
- API rate limit 是單 worker、最多 2048 client bucket 的 in-process token bucket；多 worker 或多機部署必須在 gateway 另設全域限流。
- Backend runtime 採 async 單一路徑；HTTP-facing providers、services、AgentSession tool dispatch 與 LLM client 都是 async。GTFS 更新腳本可用同步 requests，不屬於線上 API 路徑。
