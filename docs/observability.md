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

### Pipeline（`taigi_bus_agent.pipeline` meter）

| Metric 名稱 | 類型 | 單位 | 屬性 | 說明 |
|-------------|------|------|------|------|
| `pipeline.stage.duration` | Histogram | s | `pipeline.stage`, `pipeline.outcome` | 各處理階段耗時；目前有 `tts.text_process` 與 `tts.synthesize`（outcome: ok / upstream_error / timeout / connect_error） |
| `pipeline.asr.audio_bytes` | Histogram | By | — | 每次上傳音訊的 bytes 分布，用於容量規劃 |
| `pipeline.tts.audio_bytes` | Histogram | By | — | 每次回傳的合成 WAV 大小分布 |

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
from agent.telemetry import get_telemetry

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
