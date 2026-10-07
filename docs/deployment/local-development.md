# 本地開發

在自己的電腦跑後端與前端。正式主機部署見 [`production.md`](production.md)。

## 前置需求

- Python 3.12+、[uv](https://docs.astral.sh/uv/)
- Node.js + pnpm
- OpenAI-compatible LLM API：本機 [llama.cpp](local-llm-llama-cpp.md)、Ollama、vLLM，或透過 [Cloudflare Tunnel](model-services-cloudflare.md) 使用遠端模型主機
- 公車資料：TDX 金鑰與方案見 [`../tdx-tiers.md`](../tdx-tiers.md)

vLLM 的 tool calling 與非思考模式需要額外參數：

```bash
vllm serve Qwen/Qwen3.5-4B \
  --enable-auto-tool-choice --tool-call-parser hermes \
  --reasoning-parser qwen3
```

## 後端

```bash
cd backend
uv sync
cp .env.example .env
# 必填：LLM_BASE_URL、LLM_MODEL、TDX_CLIENT_ID、TDX_CLIENT_SECRET
# 管理後台寫入必填：ADMIN_TOKEN（高熵隨機值）
# Kiosk 預設為「雲林科技大學／回程」，啟動後由 /admin 修改
uv run uvicorn api:app --reload --port 8000
```

本地開發後端用 `8000`；正式環境後端用 `127.0.0.1:8080`，模型服務才佔 `8000`。若模型服務和後端在同一台機器，後端改用其他 port。

## 前端

```bash
cd frontend
pnpm install
pnpm dev
```

Vite dev server 預設把 `/api` 轉送到本機 `8000`；要換目標設 `frontend/.env` 的 `VITE_API_PROXY_TARGET`。若前端跨 origin 直接打 API，用 `API_CORS_ORIGINS` 明確開放來源。

## 路線規劃（選用）

需要本機 OTP graph、雲林 stop index 與 OTP service（預設 `http://localhost:8081`）。GTFS / stop index 更新、graph build 與 Docker 啟動見 `backend/otp/README.md`；分工與資料風險見 `docs/route-planning.md`。

## 觀測（選用）

在 `backend/.env` 設 `OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318`、`OTEL_SERVICE_NAME=taigi-bus-agent`，未設定時不送出 telemetry。SigNoz 啟動見 `backend/telemetry/README.md`，spans / metrics 與內容收集見 [`../observability.md`](../observability.md)。

## 測試與檢查

```bash
cd backend
uv run pytest
uv run ruff check .
uv run pyright
uv run pip-audit --local --ignore-vuln PYSEC-2026-597

cd frontend
pnpm typecheck
pnpm build
pnpm audit --prod --audit-level high
```

## 快速驗證

- `GET /api/departures/here`：本站路線、方向、到站 / 未發車 / 末班決策
- `GET /api/departures/routes/{route}/detail`：本站停靠路線的真實站序
- `POST /api/route-plans`：body 為 `{"destination":{"lat":..,"lng":..},"departureTime":"2026-05-22T08:00:00+08:00"}`，回傳 Kiosk 起點、目的地與 MapCN `MapRoute` 用的 `[lng, lat]` 候選路徑
