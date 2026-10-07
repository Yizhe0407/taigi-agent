# 出事時怎麼看

Kiosk 放在站牌，沒人在旁邊。哪裡慢、哪裡壞，要靠觀測才知道。

系統用 OpenTelemetry 把每一輪對話的過程送到 SigNoz：

```
使用者說話 → ASR → 規則 → LLM → 工具 → TTS
              └──────── 每一步都有耗時與結果 ────────┘
```

## 開起來

在 `backend/.env` 加兩行，不設就完全不送、不影響效能：

```dotenv
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
OTEL_SERVICE_NAME=taigi-bus-agent
```

本機啟動 SigNoz，然後開 http://127.0.0.1:8085 ：

```bash
cd backend/telemetry && docker compose up -d
```

第一次打開要先完成 SigNoz 的註冊精靈，資料才進得去。正式環境由部署腳本自動啟動，見 [正式部署](deployment/production.md) 第 5 節。

## 想知道什麼，看哪裡

| 想知道 | 看哪裡 |
|---|---|
| 這一輪為什麼慢 | Traces → `agent.turn`，展開看是 LLM、工具還是 TTS 佔最久 |
| 使用者說了什麼、系統回了什麼 | `agent.turn` 上的 `agent.input.text`、`agent.reply.text` |
| 工具查到什麼 | `agent.tool.call` 上的 `agent.tool.result` |
| 第一個聲音多久出來 | Metrics → `pipeline.voice.turn.duration` |
| 公車資料是不是被 TDX 限流 | Metrics → `provider.rate_limit`（公車 API 每次 429 記一筆）；Logs 也會有 `TDX 429` 警告 |
| Kiosk 瀏覽器端出錯（麥克風、WebRTC） | Traces → `POST /api/client-events` 的 `diagnostic` event，前端會自動回報。這類錯誤不會進 Logs |

每個 span 和 metric 的完整說明見 [觀測參考](observability-reference.md)。

## 隱私

文字內容**預設會記錄**：使用者說的話、模型回覆、工具結果，每筆截斷在 4000 字。不想記就設：

```dotenv
TELEMETRY_CAPTURE_CONTENT=false
```

原始音訊不記，只記大小。資料保留多久由 SigNoz 的設定決定；內容含使用者說的話，調整前先想清楚。
