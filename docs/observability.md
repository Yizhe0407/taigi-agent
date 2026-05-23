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
            │
            └─ httpx → TTS upstream  ← [auto] HTTPXClientInstrumentor child span

agent/session.py
    │
    ├─ LLM call spans + duration histogram   ← [manual] AgentTelemetry
    ├─ tool routing span                     ← [manual] AgentTelemetry
    └─ tool call spans + duration histogram  ← [manual] AgentTelemetry
```

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
| `agent.tool.routing` | `agent/session.py` | `agent.tool.count`, `agent.tool.names`, `agent.tool.accepted` | LLM 回傳 tool_calls 後，dispatch 前的路由點 |

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

### Pipeline（`taigi_bus_agent.pipeline` meter）

| Metric 名稱 | 類型 | 單位 | 屬性 | 說明 |
|-------------|------|------|------|------|
| `pipeline.stage.duration` | Histogram | s | `pipeline.stage`, `pipeline.outcome` | 各處理階段耗時；目前只有 `tts.text_process` 會記錄 |
| `pipeline.asr.audio_bytes` | Histogram | By | — | 每次上傳音訊的 bytes 分布，用於容量規劃 |

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

相關設定都在 `backend/agent/telemetry.py`：
- `configure_telemetry()`：初始化 TracerProvider + MeterProvider，加 OTLP exporter 與 bucket view
- 設計為 idempotent singleton；API 啟動時在 `api/__init__.py` 呼叫一次，`make_agent_session()` 內也呼叫但會直接回傳已存在的 singleton

---

## 刻意不收集的內容

以下資料不放進 spans / metrics attributes，原因是隱私或資料保留政策尚未確定：

- User input（問什麼）
- System prompt 內容
- LLM 回應文字
- Tool 回傳的完整公車資料
- 音訊內容（ASR 的 audio bytes 只記 size，不記內容）
- TTS 輸出音訊

> 若需要開啟 content-level 觀測（例如論文 trace 分析），
> 先確定資料保留期限、遮罩策略，再加對應的 span attribute 或 log exporter。

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
