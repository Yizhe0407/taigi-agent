# 可觀測性說明

本文記錄目前系統的 OpenTelemetry 覆蓋範圍、各層產生的 spans / metrics、
設定方式，以及刻意不收集的內容。

---

## 架構概覽

```
HTTP request
    │
    ▼
FastAPIInstrumentor          ← [auto] 每個 route 一個 server span
    │
    ├─ api/asr.py
    │       │
    │       └─ httpx → ASR upstream ← [auto] HTTPXClientInstrumentor child span
    │
    └─ api/tts.py
            │
            ├─ tts.text_process span  ← [manual] HanloFlow + Taibun 純 Python 階段
            │                            （在專用 thread 執行，span 包住 await）
            ├─ tts.synthesize span    ← [manual] 並發呼叫 TTS upstream 的彙總階段
            │
            └─ httpx → TTS upstream  ← [auto] HTTPXClientInstrumentor child span

agent/session.py
    │
    ├─ agent.turn span + turn counter        ← [manual] AgentTelemetry
    ├─ LLM call spans + duration histogram   ← [manual] AgentTelemetry
    ├─ tool routing span                     ← [manual] AgentTelemetry
    ├─ tool call spans + duration histogram  ← [manual] AgentTelemetry
    └─ diagnostic span events                ← [manual] log_diagnostic（retry / context trim）

providers / services 快取
    └─ provider.cache.lookup counter         ← [manual] ebus route_info / route_estimate、
                                                TDX token、Moovo stations 的 hit/miss

voice/（pipecat WebRTC 語音 pipeline，api/voice.py 的 SmallWebRTCRequestHandler 啟動）
    │
    ├─ voice/pipeline.py
    │       ├─ pipeline.voice.session counter + active_sessions UpDownCounter
    │       │       ← [manual] on_client_connected / on_client_disconnected event handler
    │       ├─ pipeline.voice.barge_in counter
    │       │       ← [manual] BargeInProcessor，使用者於 bot 說話中打斷時
    │       └─ diagnostic span events         ← [manual] log_diagnostic（disconnect 取消 pipeline、
    │                                            run_voice_pipeline 內未預期例外、_start_pipeline
    │                                            背景 task 失敗，見 api/voice.py）
    ├─ voice/stt_breeze.py
    │       ├─ pipeline.asr.audio_bytes histogram（既有）
    │       └─ asr.transcript content         ← 掛在當前 span（既有）
    ├─ voice/agent_processor.py
    │       └─ TurnLatencyTracker.mark_transcription()（收到 TranscriptionFrame 時起算）
    └─ voice/tts_taigi.py
            ├─ pipeline.stage.duration（stage="voice.tts"）← [manual] 每次 run_tts() 呼叫
            └─ TurnLatencyTracker.mark_first_audio()（第一個 TTSAudioRawFrame yield 時停表，
               記錄 pipeline.voice.turn.duration）
```

> 所有 upstream HTTP 走 `providers/http.py` 的共用 `httpx.AsyncClient`，
> `HTTPXClientInstrumentor` 是全域 instrument，共用 client 一樣會自動產生 child span。

---

## Spans

### 自動（auto-instrumentation）

| Span 名稱 | 產生者 | 關鍵屬性 |
|-----------|--------|----------|
| `POST /api/asr` | `FastAPIInstrumentor` | `http.route`, `http.response.status_code` |
| `POST /api/tts` | `FastAPIInstrumentor` | `http.route`, `http.response.status_code` |
| `POST /api/chat/sessions/{sessionId}/messages` | `FastAPIInstrumentor` | `http.route`, `http.response.status_code` |
| `POST {asr_upstream}/v1/audio/transcriptions` | `HTTPXClientInstrumentor` | `server.address`, `http.request.method`, `http.response.status_code` |
| `POST {tts_upstream}/v1/audio/speech` | `HTTPXClientInstrumentor` | `server.address`, `http.request.method`, `http.response.status_code` |

### 手動（manual instrumentation）

| Span 名稱 | 所在檔案 | 屬性 | 說明 |
|-----------|----------|------|------|
| `tts.text_process` | `api/tts.py` | `tts.input_chars` | HanloFlow + Taibun 轉換，此段無 HTTP，HTTPXClientInstrumentor 無法自動偵測 |
| `tts.synthesize` | `api/tts.py` | `tts.segments` | 並發送出全部 Tailo 分段至 TTS upstream 的彙總階段（個別 HTTP 由 HTTPX auto-instrumentation 補） |
| `agent.tool.routing` | `agent/session.py` | `agent.tool.count`, `agent.tool.names`, `agent.tool.accepted` | LLM 回傳 tool_calls 後，dispatch 前的路由點 |

### Span events

`agent/diagnostics.py` 的 `log_diagnostic()` 除了印 stdout，也會在當前 span 上掛
`diagnostic` event（`diagnostic.scope` + `diagnostic.message`），讓 LLM retry、
context trim、tool round limit 這類運維訊息直接出現在 trace 時間軸上。
訊息僅含運維內容（不含 user input / prompt）。

> `AgentTelemetry.start_span()` 在當前 trace context 下建立 child span，
> 所以 `tts.text_process` 會自動成為 FastAPI request span 的子節點。

---

## Metrics

### Agent（`taigi_bus_agent.agent` meter）

| Metric 名稱 | 類型 | 單位 | 屬性 | 說明 |
|-------------|------|------|------|------|
| `agent.llm.duration` | Histogram | s | `agent.llm.model`, `agent.llm.operation`, `agent.outcome` | 單次 LLM request 耗時 |
| `agent.llm.retry` | Counter | {retry} | `agent.llm.operation`, `error.type` | LLM retry 次數 |
| `agent.tool.duration` | Histogram | s | `agent.tool.name`, `agent.outcome` | 單次 tool handler 耗時 |
| `agent.tool.error` | Counter | {error} | `agent.tool.name`, `error.type` | Tool dispatch / handler 失敗 |
| `agent.turn` | Counter | {turn} | `agent.router.path`（canned/llm）, `agent.router.intent`, `agent.outcome`（ok/tool_round_limit/error） | 完成的對話輪數；canned vs LLM 比例與失敗率直接可畫 |
| `agent.turn.tool_rounds` | Histogram | {round} | `agent.outcome` | 單輪 LLM 路徑消耗的 tool-call 輪數；分布右移代表 prompt 或工具描述退化 |

### Provider（`taigi_bus_agent.provider` meter）

| Metric 名稱 | 類型 | 單位 | 屬性 | 說明 |
|-------------|------|------|------|------|
| `provider.cache.lookup` | Counter | {lookup} | `cache.name`（ebus.route_info / ebus.route_estimate / tdx.token / moovo.stations）, `cache.outcome`（hit/miss） | 上游資料快取命中率；調 TTL 與評估 upstream 負載的依據 |
| `provider.fallback` | Counter | {call} | `provider.operation`（eta/route_estimate）, `provider.outcome`（ebus_hit/tdx_fallback/both_empty） | `HybridBusProvider` 的 ebus→TDX fallback 結果；ebus 悄悄壞掉會直接反映成 tdx_fallback、進而 both_empty 比例上升，是 ebus 健康度的先行指標 |

### Departures（`taigi_bus_agent.departures` meter）

| Metric 名稱 | 類型 | 單位 | 屬性 | 說明 |
|-------------|------|------|------|------|
| `departures.decision` | Counter | {decision} | `departures.decision`（arriving_soon/can_wait/long_wait/not_departed/last_departed/unknown/filtered_terminal_direction） | `_classify_stop` 產出的決策分類分布，加上 `_is_terminal_direction()` auto-filter 過濾掉的筆數（`filtered_terminal_direction`）；後者異常偏高代表方向過濾過激，值得回頭核對站牌設定 |

### Pipeline（`taigi_bus_agent.pipeline` meter）

| Metric 名稱 | 類型 | 單位 | 屬性 | 說明 |
|-------------|------|------|------|------|
| `pipeline.stage.duration` | Histogram | s | `pipeline.stage`, `pipeline.outcome` | 各處理階段耗時；REST 路徑有 `tts.text_process` 與 `tts.synthesize`（outcome: ok / upstream_error / timeout / connect_error）；WebRTC 語音路徑有 `voice.tts`（outcome: ok / error / timeout / cancelled，見下方說明） |
| `pipeline.asr.audio_bytes` | Histogram | By | — | 每次上傳音訊的 bytes 分布，用於容量規劃 |
| `pipeline.tts.audio_bytes` | Histogram | By | — | 每次回傳的合成 WAV 大小分布 |
| `pipeline.voice.session` | Counter | {session} | `pipeline.outcome`（connected/disconnected） | WebRTC 語音 session 連線/斷線事件，掛在 `voice/pipeline.py` 的 `on_client_connected` / `on_client_disconnected` |
| `pipeline.voice.active_sessions` | UpDownCounter | {session} | — | 目前存活的 WebRTC 語音 session 數；connected +1、disconnected -1 |
| `pipeline.voice.barge_in` | Counter | {interruption} | — | 使用者於 bot 說話中打斷次數，記在 `BargeInProcessor` 實際呼叫 `broadcast_interruption()` 之處（VAD 誤觸發但 bot 靜默時不算，見 gate 邏輯註解） |
| `pipeline.voice.turn.duration` | Histogram | s | — | 語音輪延遲：`voice/agent_processor.py` 收到 `TranscriptionFrame`（STT 產出文字）起算，到 `voice/tts_taigi.py` `run_tts()` yield 出第一個 `TTSAudioRawFrame` 停表。量測點刻意選在「第一個音訊 frame 產生」而非「送達瀏覽器」——後者還要經過 pipecat 內部 queue 與 aiortc 編碼，量到這裡足以反映 agent 推理 + TTS 首段合成的延遲，不需額外跨層打點 |

> **Duration histograms** (`*duration*`)：套用 OTel HTTP semconv 建議的 bucket boundaries：
> `[0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.25, 0.5, 0.75, 1, 2.5, 5, 7.5, 10]` 秒
>
> Wildcard 刻意覆蓋 auto-instrumented 的 `http.server.request.duration`（FastAPI）與
> `http.client.request.duration`（HTTPX），同一套 semconv buckets 均適用。
>
> **Byte histograms** (`*bytes*`)：套用 KB→25MB 的 bucket boundaries：
> `[1KB, 4KB, 16KB, 64KB, 256KB, 1MB, 4MB, 10MB, 25MB]`
>
> 原因：OTel 預設 bucket 頂端為 10 000（適合計數），對 MB 等級的音訊上傳幾乎沒有解析度，
> P99 會全落在最後一個 overflow bucket，喪失分布資訊。

> **`voice.tts` 的 outcome 詞彙**：比 REST `tts.synthesize` 多一個 `cancelled`——
> WebRTC 語音路徑常態性地被 barge-in 中途取消（正常操作，不是錯誤），
> 混進 `error` 會污染錯誤率儀表板，所以獨立成一個 outcome 值。
>
> **idle timeout 沒有訊號**：`voice/pipeline.py` 的 `PipelineWorker` 建立時
> 明確傳 `idle_timeout_secs=None`（Phase 4 深度審查發現 300s 預設值會殺掉
> 仍在通話中的連線，見 `tasks/pipecat_webrtc_plan.md`），所以目前這條路徑
> 不會觸發，未加 `log_diagnostic`。若未來重新啟用 idle timeout，掛點是
> `PipelineWorker` 的 `on_idle_timeout` 回呼。
>
> **`pipeline.voice.turn.duration` 的已知限制**：量測靠 `TurnLatencyTracker`
> 這個單一可變時間戳，由 `voice/pipeline.py` 建立、注入 `agent_processor` 與
> `tts_taigi` 兩個 processor 共用。對「一問一答不重疊」的常見情況是準確的；
> 若使用者在上一輪 TTS 尚未產出音訊前又觸發新一輪 transcription（barge-in
> 疊加新語句），時間戳會被新的 `mark_transcription()` 覆蓋，舊樣本可能遺失或
> 量到偏短的數字。目前流量下影響小，若之後發現分布異常，再升級成 per-turn ID。

---

## 設定

OTLP endpoint 由環境變數控制；**不設定則所有 telemetry 靜默丟棄**，不影響效能。

```env
# 任一個有值即啟用 OTLP exporter
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318   # SigNoz OTLP/HTTP 預設 port
OTEL_SERVICE_NAME=taigi-bus-agent                   # 顯示在 SigNoz 的服務名稱
```

相關設定都在 `backend/telemetry.py`（backend 根層級的中立 infra 模組，
agent / api / providers / services 都可引用，不構成跨層依賴）：
- `configure_telemetry()`：初始化 TracerProvider + MeterProvider，加 OTLP exporter 與 bucket view
- 設計為 idempotent singleton；API 啟動時在 `api/__init__.py` 呼叫一次，`make_agent_session()` 內也呼叫但會直接回傳已存在的 singleton

> 測試不外送 telemetry：`backend/tests/conftest.py` 會把 `OTEL_EXPORTER_OTLP_*`
> 設成空字串（`load_dotenv` 不覆寫既有變數），避免 `.env` 的 endpoint 漏進
> pytest 觸發 exporter 重試迴圈。

---

## Content-level 觀測

文字層級的內容**預設收集**，掛在對應 span 的 attributes 上；
設 `TELEMETRY_CAPTURE_CONTENT=false` 可整批關閉（metrics 與 span 結構不受影響）。
所有內容經 `AgentTelemetry.set_content()` 統一截斷（預設 4000 字元，
LLM messages 8000），超過上限以 `…[truncated N chars]` 結尾。

| Attribute | 所在 span | 內容 |
|-----------|-----------|------|
| `agent.input.text` | `agent.turn` | User input（canned 與 LLM 路徑都有） |
| `agent.reply.text` | `agent.turn` | 最終回覆文字 |
| `agent.system_prompt` | `agent.turn` | System prompt（LLM 路徑） |
| `gen_ai.input.messages` | `agent.llm.call` | 該次 request 的完整 messages JSON |
| `gen_ai.output.messages` | `agent.llm.call` | Completion content + tool_calls JSON |
| `agent.tool.arguments` | `agent.tool.call` | Tool call 原始 arguments JSON |
| `agent.tool.result` | `agent.tool.call` | Tool 回傳字串（公車資料） |
| `asr.transcript` | `POST /api/asr` request span | 語音辨識結果文字 |
| `tts.input_text` / `tts.hanlo_text` / `tts.tailo_text` | `tts.text_process` | TTS 三階段文字 |

仍然不收集：

- **原始音訊 bytes**（ASR 上傳、TTS 輸出）— binary 塞 span attribute 是反模式
  （attribute 大小限制、collector 記憶體壓力）；大小分布已由
  `pipeline.asr.audio_bytes` / `pipeline.tts.audio_bytes` 涵蓋。

> 資料保留期限跟隨 SigNoz（或所用 backend）的 trace retention 設定；
> 內容含使用者語句，調整 retention 前先確認需求。

---

## 新增 pipeline 階段的作法

在 `backend/api/tts.py` 的 `tts.text_process` 是標準範例：

```python
import time
from telemetry import get_telemetry

t0 = time.perf_counter()
try:
    with get_telemetry().start_span("your.stage.name", {"your.attr": value}):
        result = do_something()
    get_telemetry().record_pipeline_stage(
        time.perf_counter() - t0, stage="your.stage.name", outcome="ok"
    )
except Exception as err:
    get_telemetry().record_pipeline_stage(
        time.perf_counter() - t0, stage="your.stage.name", outcome="error"
    )
    raise
```

`get_telemetry()` 在 OTLP 未設定時回傳 NoOp instance，呼叫方不需要 guard。

---

## 前端錯誤回報（client events）

Kiosk 無人值守，麥克風權限失敗、WebRTC ICE failed、未捕捉例外過去後端完全看不到。
`POST /api/client-events`（`backend/api/client_events.py`）接收前端上報，
驗證後轉呼叫 `log_diagnostic(scope="client", ...)`——出現在 stdout 與當前
FastAPI request span 的 `diagnostic` event 上，看法同「Span events」一節。

- Body：`{type, message, detail?, ts?}`；`message` 截斷至 500 字、`detail` 截斷至 2000 字。
- 前端全域掛點：`frontend/src/main.ts` 的 `window.onerror` / `unhandledrejection` /
  `app.config.errorHandler`；WebRTC 失敗路徑在
  `frontend/src/features/agent-chat/composables/useWebRTC.ts`
  （mic 拒絕、ICE failed、offer 送出失敗、SDP 應用失敗）。
- 送出方式：`frontend/src/lib/report-client-event.ts`，`navigator.sendBeacon`
  優先、失敗時退回 `fetch(..., { keepalive: true })`。
- 防洪（僅前端，client-side best effort）：同一 `type:message` 60 秒內去重、
  每分鐘最多送 10 筆，超過直接丟棄，不重試、不排隊。
- 不是 RUM 平台：無聚合、無 dashboard，純粹讓錯誤出現在既有 trace/log 管線。
