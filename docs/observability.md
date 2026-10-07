# 出事時怎麼看

Kiosk 放在站牌，沒人在旁邊。哪裡慢、哪裡壞，要靠觀測才知道。

系統用 OpenTelemetry 把每一輪對話的過程送到 SigNoz：

```
使用者說話 → ASR → 路由 → LLM → 工具 → TTS
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

正式環境由部署腳本自動啟動，設定見 [正式部署](deployment/production.md) 第 5 節。

## 能看到什麼

| 想知道 | 看哪裡 |
|---|---|
| 這一輪為什麼慢 | Traces → `agent.turn`，往下展開看是 LLM、工具還是 TTS |
| 使用者實際說了什麼、系統回了什麼 | `agent.turn` 的 `agent.input.text` / `agent.reply.text` |
| 工具查到什麼 | `agent.tool.call` 的 `agent.tool.result` |
| 語音第一個聲音多久出來 | Metrics → `pipeline.voice.turn.duration` |
| 是不是被 TDX 限流 | `provider.cache.lookup` 命中率、provider 的錯誤 log |
| Kiosk 瀏覽器端壞了（麥克風、WebRTC） | Logs，`scope=client`，前端會自動回報 |

完整的 span 與 metric 清單見 [觀測參考](observability-reference.md)。

## 隱私

文字內容**預設會記錄**（使用者語句、模型回覆、工具結果），每筆截斷在 4000 字。不想記就設：

```dotenv
TELEMETRY_CAPTURE_CONTENT=false
```

原始音訊不會記，只記大小。資料保留多久由 SigNoz 的設定決定，調整前先想清楚：內容含使用者說的話。
