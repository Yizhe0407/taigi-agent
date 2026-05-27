# 架構與目錄

本專案以固定站牌 Kiosk 為場域。後端提供即時離站決策、聊天 tool facade、路線規劃 API、ASR/TTS proxy；前端提供 Kiosk dashboard、PIP 數位站務員與地圖路線規劃。

## 核心邊界

- `AgentSession` 是 harness orchestration，不放公車領域邏輯。
- 公車資料來源集中在 provider，分類與決策集中在 service，Agent 看到的是 tool facade 回傳的字串。
- 路線規劃不是聊天文字 tool；前端確認目的地座標後呼叫 `POST /api/route-plans`。
- Context compact 會把 transcript 與完整長 tool result 保存到 `.agent_state/`，active context 只保留摘要、路徑與預覽。
- Telemetry 只記 model、tool name、outcome、error type、latency 等 metadata，預設不記內容。

## 後端

```text
backend/
  main.py          # CLI 入口：load_dotenv() -> agent.loop.run()
  config.py        # Settings + make_agent_session；API 與 CLI 共用
  api/             # FastAPI app 與 HTTP endpoints
  agent/           # Agent harness、LLM client、tool dispatch、prompt、context、telemetry
  pipeline/        # Mandarin -> HanloFlow -> Taibun 等文字處理 pipeline
  providers/       # 外部資料來源 adapter：ebus、OTP、TDX Moovo
  services/        # 領域模型、分類、決策、provider facade
  tools/           # Agent 可見的 str facade
  scripts/         # GTFS / stop metadata 更新流程
  otp/             # 本機 OTP build input / graph；git 只保留資料夾與說明
  tests/
```

### API

- `api/__init__.py`：FastAPI app、CORS、router include、telemetry setup。
- `api/chat.py`：`/api/chat/*`，SQLite-backed `ChatSessionStore`。
- `api/departures.py`：`/api/departures/here` 與路線詳情。
- `api/route_plans.py`：`/api/route-plans` 與 `/api/kiosk`。
- `api/moovo.py`：`/api/moovo/*`。
- `api/asr.py`：Qwen3-ASR proxy。
- `api/tts.py`：HanloFlow -> Taibun -> Piper TTS proxy。

### Agent

- `agent/session.py`：messages、tool-call loop、context recovery。
- `agent/loop.py`：CLI I/O，呼叫 `AgentSession`。
- `agent/llm_client.py`：OpenAI-compatible LLM call、retry/backoff、context overflow。
- `agent/tool_dispatch.py`：tool call parse 與 dispatch。
- `agent/tools.py`：`TOOL_SCHEMAS` 與 `TOOL_HANDLERS`。
- `agent/context.py`：token budget、ContextStore、transcript / 長 tool result compact。
- `agent/telemetry.py`：OpenTelemetry spans / metrics。

### 領域層

- `providers/bus.py`：`BusProvider` Protocol。
- `providers/yunlin_ebus.py`：雲林 ebus provider。
- `providers/otp.py`：OpenTripPlanner GraphQL provider。
- `providers/moovo.py`：TDX bike provider。
- `services/departures.py`：離站決策唯一分類來源，支援 provider override。
- `services/route_plans.py`：OTP 路線規劃 facade、Kiosk 起點、雲林邊界、view model。
- `services/moovo.py`：公共自行車站 dataclass、解析、cache、距離查詢。
- `services/stop_catalog.py`：TDX / GTFS 更新流程產生的雲林 stop index。
- `services/yunlin_boundary.py`：雲林縣 GeoJSON point-in-polygon。
- `tools/kiosk_bus.py`：Agent str facade、站名縮寫、`KIOSK_STOP` / direction、prefetch。

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
- `features/departures/`：離站決策首頁、路線詳情、route colors、輪詢與顯示狀態。
- `features/route-planner/`：MapCN destination picker、路線規劃 request、指定時間 wheel。
- `features/agent-chat/`：PIP 對話 session / send / scroll 狀態。
- `components/ui/`：shadcn-vue 與 MapCN Vue copy-paste UI components。

## 已知技術債

- ebus 後端介面不是本專題控制的公開契約；若 payload 改版，主要修補點是 `backend/providers/yunlin_ebus.py`。
- Compact 後的完整內容暫無 retrieval tool；若要讓 agent 主動回讀壓縮資料，需要明確 retrieval tool 與資料保留策略。
- Chat session 持久化在 `.agent_state/sessions.db`，目前仍綁單機檔案；scale out 需改外部 KV / Redis。
- Backend runtime 採 async 單一路徑；HTTP-facing providers、services、AgentSession tool dispatch 與 LLM client 都是 async。GTFS 更新腳本可用同步 requests，不屬於線上 API 路徑。
