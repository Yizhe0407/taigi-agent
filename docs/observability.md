# 觀測

系統以 OpenTelemetry 記錄每一輪對話各步驟的耗時與結果，送到 SigNoz 查看。

```
使用者說話 → ASR → 規則 → LLM → 工具 → TTS
```

## 啟用

在 `backend/.env` 加入以下兩行。未設定時不送出任何資料。

```dotenv
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
OTEL_SERVICE_NAME=taigi-bus-agent
```

本機啟動 SigNoz，UI 在 http://127.0.0.1:8085 ：

```bash
cd backend/telemetry && docker compose up -d
```

第一次開啟需完成 SigNoz 的註冊精靈，之後資料才能寫入。正式環境由部署腳本啟動，見 [正式部署](deployment/production.md) 第 5 節。

## 常見查詢

| 要查的事 | 位置 |
|---|---|
| 某一輪回應為什麼慢 | Traces → `agent.turn`，展開查看 LLM、工具、TTS 各自的耗時 |
| 使用者說了什麼、系統回了什麼 | `agent.turn` 的 `agent.input.text`、`agent.reply.text` |
| 工具查到的內容 | `agent.tool.call` 的 `agent.tool.result` |
| 第一個聲音的延遲 | Metrics → `pipeline.voice.turn.duration` |
| 公車資料是否被 TDX 限流 | Metrics → `provider.rate_limit`（公車 API 每次 429 記一筆）；Logs 中的 `TDX 429` 警告 |
| Kiosk 瀏覽器端的錯誤（麥克風、WebRTC） | Traces → `POST /api/client-events` 的 `diagnostic` event。這類錯誤不會出現在 Logs |

所有 span 與 metric 見 [觀測參考](observability-reference.md)。

## 隱私

文字內容預設會記錄，包括使用者說的話、模型回覆與工具結果，每筆截斷在 4000 字。關閉方式：

```dotenv
TELEMETRY_CAPTURE_CONTENT=false
```

原始音訊不記錄，只記錄大小。資料保留期限由 SigNoz 設定決定。內容包含使用者說的話，調整保留期限前，先確認這些資料可以保存多久。
