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

- `api/__init__.py`：FastAPI app、CORS、router include、telemetry setup，以及全域 request body middleware。POST/PUT/PATCH 預設上限 64 KB；`/api/asr` 含 multipart overhead 上限 26 MB，chunked body 也在串流接收時累計並回 413。
- `api/admin.py`：`/api/admin/kiosk`（GET/PUT）與 `/api/admin/stops`（GET）；寫入採 `ADMIN_TOKEN` fail-closed 驗證，且只接受站名與方向。座標由後端 stop catalog 的主要群集計算，不能由前端覆寫。
- `api/chat.py`：`/api/chat/*`，SQLite-backed `ChatSessionStore`。`respond_in_session_stream` 為唯一實作路徑（voice 與 SSE 共用）；`get_store()` 是 process-wide store 的公開存取點，voice（`api/voice.py`、`voice/agent_processor.py`）與 SSE 都經它取得同一個 store，不跨層 import 底線符號。`POST /api/chat/sessions/{id}/messages/stream` 以 SSE 推 `{delta}`/`{done}`/`{error}` 事件；非串流 endpoint 已移除。
- `api/departures.py`：`/api/departures/here` 與路線詳情；`GET /api/departures/stream` SSE 推播——ETA warmup loop（25 s）每次刷新 cache 後 `notify_snapshot_refreshed()` 喚醒連線推最新 snapshot，40 s fallback 自刷新兜底。兩個 GET 端點刻意不掛 RateLimit：是 kiosk 自家高頻主路徑，且 `services.departures` 對底層 snapshot 已有 25 s cache。
- `api/route_plans.py`：`/api/route-plans` 與 `/api/kiosk`（含 direction）。
- `api/moovo.py`：`/api/moovo/*`。
- `api/asr.py`：`/api/asr` proxy，config 讀取與 upstream 呼叫委派給 `providers/asr.py`（文字模式與語音模組共用）；音檔以 1 MB chunk 讀取，實際檔案內容上限 25 MB。
- `api/tts.py`：`/api/tts`，呼叫 `services/taigi_tts.py` 的共用 pipeline（見下）後轉成 WAV `Response`。Tailo 最多 64 段、每段最多 500 字元、同時最多 4 個 upstream request；單請求 15 秒、整次合成 45 秒。
- `api/voice.py`：`/api/voice/offer`，處理 WebRTC SDP 交換並在背景啟動語音 pipeline，支援 session_id 綁定。session 解析失敗時回 500 並關閉該 peer connection——pipecat 會吞掉 callback 例外並照樣發 SDP answer，不主動關就會留下沒有 pipeline 的孤兒 pc。
- `api/sse.py`：共用 SSE 樣板（`SSE_HEADERS`、`sse_event()`），供 `api/chat.py` 與 `api/departures.py` 共用。

### Voice Pipeline (WebRTC)

- `voice/pipeline.py`：Pipecat 語音管線組裝（SmallWebRTCTransport、VAD、中斷處理）與連線生命週期管理。`SubtitleSyncProcessor` 掛在 `transport.output()` 之後，攔自訂 `SubtitleFrame`（`tts_taigi.run_tts` 在音訊 frames **之前** yield，帶該段精確音訊時長；不可繼承 TTSTextFrame、不可設 pts）——事件到達 ≈ 段起播，前端在 durationMs 內線性逐字揭示，全文 `agent_reply` 由 `bot_silent` 收尾補全。注意：pipecat 預設段尾 TTSTextFrame 保持存在（`push_text_frames=False` 會觸發 WordCompletionTracker 段尾補發全文，是坑），無人消費即可。
- `voice/stt_breeze.py`：繼承 Pipecat `SegmentedSTTService`，搭配 VAD 收集完整語句後呼叫 `providers/asr.py` 轉文字（與 `api/asr.py` 共用同一 provider，不 import `api/`）。
- `voice/agent_processor.py`：將原本的 `AgentSession` 封裝為 Pipecat 的 `FrameProcessor`，介接文字與語音的資料流。消費 `respond_in_session_stream` 逐 chunk 推 `TextFrame`——Pipecat TTS 句子聚合器在 LLM 還在生成時就開始逐句合成，首音延遲不再等完整回覆。`_open_stream()` 把 session 過期重試（最多兩次：先呼叫、LookupError 則 `get_store().create()` 重建再呼叫一次）收斂成一個 helper。
- `voice/tts_taigi.py`：繼承 Pipecat `TTSService`，呼叫 `services/taigi_tts.py` 的共用 pipeline 取得 pre-decode 結果後自行解成 PCM 音訊流供 WebRTC 播放（`api/tts.py` 解成 WAV `Response`）。

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
- `providers/ebus.py`：ebus.yunlin.gov.tw `BusProvider` 實作。首次 miss 以 20 個並行 request 掃描路線，一次建立所有精確站名的 route index；完整索引原子寫入 `.agent_state/ebus-route-index.json` 並保留 24 小時，重新啟動不必重掃。route estimate 結果快取 30 s，並發 miss 以 per-key lock 合流。`fetch_eta_rows_for_stop` 回傳 `list | None`：`None` = 全部查詢失敗（供 hybrid fallback 判斷），`[]` = 成功但無資料。
- `providers/tdx_bus.py`：TDX `BusProvider` 實作。同時查 `City/YunlinCounty`（市區公車）與 `InterCity`（公路客運）兩個 endpoint 並合併。OAuth2 token 自動快取，route estimate 採 256-entry LRU；ETA 與 route estimate 的並發 miss 皆以 per-key lock 合流避免 429 cascade。StopOfRoute 單邊 endpoint 失敗時結果以 60 s partial TTL 快取（正常 600 s）。route_id 以 SubRouteName string 為主鍵。
- `providers/hybrid.py`：`HybridBusProvider`，線上唯一 `BusProvider` runtime 實例。`load_route_info`、ETA 與 route estimate 以 ebus 為主、TDX 為備援；ETA 與 route estimate 都以 ebus 的 `None`-vs-`[]` sentinel 區分「查詢失敗才 fallback TDX」與「成功但無資料不 fallback」。`fetch_routes_at_stop` 直接使用 TDX，ebus 站名缺字時也由 TDX 補終點名稱。
- `providers/otp.py`：OpenTripPlanner GraphQL provider。
- `providers/moovo.py`：TDX bike provider。
- `providers/asr.py`：ASR upstream provider（config 讀取 + multipart 上傳），供 `api/asr.py` 與 `voice/stt_breeze.py` 共用，兩邊都不再互相 import 私有符號。
- `services/taigi_tts.py`：TTS config、Tailo 切段、`synthesize_segments` 有界並發派送；`prepare_tailo()` 收斂 normalize 後→text-process→split 的共用序列（回傳解碼前的 hanlo/tailo/segments），`api/tts.py` 與 `voice/tts_taigi.py` 各自接手 `synthesize_segments` 的錯誤轉換與音訊解碼（WAV vs PCM）。`make_silence_pcm()` 是兩邊共用的靜音位元組運算。
- `services/kiosk_config.py`：Runtime kiosk 設定 singleton（stop_name、direction、lat/lon）；先原子落盤再發布記憶體狀態，並用 mtime 觀察其他 worker 的更新。持久化至 `.agent_state/kiosk_config.json`，預設雲林科技大學／回程。
- `services/departures/`：離站決策唯一分類來源，支援 provider override。方向過濾分兩層：admin 設定「去程」或「回程」時直接照設定過濾（不做 auto-detect）；設定「去回程都有」（go_back=None）時啟動 `_is_terminal_direction()` 自動過濾「本站是該方向終點（即抵達非出發）」的方向，循環路線（go_dest == back_dest == 本站）不過濾。`_classify_stop` 讀 TDX `stop_status` / `estimate_seconds`，回傳 `StopClassification` dataclass，所有 render 函式共用同一分類規則。方向編碼 0=去程、1=回程（TDX Direction）。`route_id` 全層為 str（SubRouteName）。查無路線/目的地時，renderer 回傳候選清單（路線用 `route_info` 站牌路線表；目的地用 `fuzzy_match._fuzzy_candidates`），交給 LLM 挑音近者重查（ASR 聽錯救援，見 `agent/prompt.py`【聽錯救援】）。套件內部分三層：`normalize.py` 共用正規化原語（`TAIPEI_TZ`、`_strip_paren`、`_name_matches` 等）、`fuzzy_match.py` ASR 聽錯救援比對、`rows.py` TDX row 整形（scope 過濾、去重、下游站推導）；後兩者只依賴 `normalize.py`，彼此不互相 import。
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

## 已知技術債

- TDX API 與 ebus API 都是外部契約；TDX 欄位或 endpoint 改版修 `providers/tdx_bus.py`，ebus 改版修 `providers/ebus.py`，路由邏輯改版修 `providers/hybrid.py`。
- Chat session 持久化在 `.agent_state/sessions.db`，目前仍綁單機檔案；scale out 需改外部 KV / Redis。
- API rate limit 是單 worker、最多 2048 client bucket 的 in-process token bucket；多 worker 或多機部署必須在 gateway 另設全域限流。
- Backend runtime 採 async 單一路徑；HTTP-facing providers、services、AgentSession tool dispatch 與 LLM client 都是 async。GTFS 更新腳本可用同步 requests，不屬於線上 API 路徑。
