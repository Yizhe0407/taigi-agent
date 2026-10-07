# 在自己的電腦上跑起來

要上正式主機，看 [production.md](production.md)。

## 需要先有

- Python 3.12+、[uv](https://docs.astral.sh/uv/)、Node.js + pnpm
- [process-compose](https://github.com/F1bonacc1/process-compose)（`brew install process-compose`）
- 一個 OpenAI 相容的 LLM：本機 [llama.cpp](local-llm-llama-cpp.md)、Ollama、vLLM，或透過 [Cloudflare Tunnel](model-services-cloudflare.md) 用遠端模型主機
- TDX 金鑰（免費方案很快會用完，見 [tdx-tiers.md](../tdx-tiers.md)）
- 選用：Docker，要跑路線規劃或觀測才需要

## 啟動

先設定後端環境變數：

```bash
cp backend/.env.example backend/.env
# 必填：LLM_BASE_URL、LLM_MODEL、TDX_CLIENT_ID、TDX_CLIENT_SECRET
# 後台寫入必填：ADMIN_TOKEN（高熵隨機值）
```

然後在 repo root：

```bash
process-compose up backend frontend
```

它會裝好依賴、啟動後端 `:8000`，等後端健康了再啟動前端。開 Vite 印出的網址就能看到離站首頁。

不加參數的 `process-compose up` 會再多開兩個 docker 服務：路線規劃用的 OTP 和觀測用的 SigNoz。沒有 Docker、或還沒建好 OTP graph，這兩個會顯示失敗，但不影響後端和前端。

process-compose 自己的本機設定（例如 `PC_PORT_NUM`）放在 `.pc_env`，已被 git 忽略。

### 不用 process-compose

開兩個終端機：

```bash
cd backend && uv sync && uv run uvicorn api:app --reload --port 8000
```

```bash
cd frontend && pnpm install && pnpm dev
```

## 會碰到的設定

- Vite 預設把 `/api` 轉給本機 `8000`；要換目標，設 `frontend/.env` 的 `VITE_API_PROXY_TARGET`。前端若跨 origin 直接打 API，用 `API_CORS_ORIGINS` 開放來源。
- 模型服務和後端在同一台時，`8000` 讓給模型，後端改用別的 port（正式環境後端就是用 `127.0.0.1:8080`）。
- 用 vLLM 的話，tool calling 要加 `--enable-auto-tool-choice --tool-call-parser hermes --reasoning-parser qwen3`。

## 路線規劃（選用）

要先建好 OTP graph，服務才有東西可跑，位址預設 `http://localhost:8081`。步驟見 [backend/otp/README.md](../../backend/otp/README.md)，分工見 [route-planning.md](../route-planning.md)。

## 觀測（選用）

在 `backend/.env` 設 `OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318`，不設就不送。UI 在 http://127.0.0.1:8085。怎麼看資料見 [observability.md](../observability.md)，SigNoz 本身見 [backend/telemetry/README.md](../../backend/telemetry/README.md)。

## 確認有通

先打這幾個 API：

- `GET /api/departures/here`：本站每條路線的狀態
- `GET /api/departures/routes/{route}/detail`：某條路線的完整站序
- `POST /api/route-plans`：body 是 `{"destination":{"lat":..,"lng":..},"departureTime":"2026-05-22T08:00:00+08:00"}`，回傳地圖可畫的候選路線

要看小芸整段對話怎麼走，不用開前端也不用講話：

```bash
cd backend && uv run python e2e_test.py
```

它用真的 LLM 和工具跑一段寫好的多輪對話（固定回覆、各工具、閒聊都有），把每一輪印出來給你看。沒有斷言，靠人讀。

## 測試與檢查

```bash
cd backend
uv run pytest
uv run ruff check .
uv run pyright
uv run pip-audit --local --ignore-vuln PYSEC-2026-597
```

```bash
cd frontend
pnpm test
pnpm typecheck
pnpm build
pnpm audit --prod --audit-level high
```

要重現 TDX 限流、上游失敗、快取過期，不要去打真的 TDX：`TdxBusProvider` 可以注入假時鐘（`clock=`）和限速器，HTTP 用 monkeypatch 換成假的回應，寫法照 `backend/tests/providers/test_tdx_bus.py` 的 `_patch_http`。
