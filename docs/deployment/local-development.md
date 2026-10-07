# 本地開發

在本機啟動後端與前端。正式環境見 [production.md](production.md)。

## 需要的工具

- Python 3.12+、[uv](https://docs.astral.sh/uv/)、Node.js + pnpm
- [process-compose](https://github.com/F1bonacc1/process-compose)（`brew install process-compose`）
- OpenAI 相容的 LLM：本機 [llama.cpp](local-llm-llama-cpp.md)、Ollama、vLLM，或透過 [Cloudflare Tunnel](model-services-cloudflare.md) 使用遠端模型主機
- TDX 金鑰（免費方案的額度很快會用完，見 [tdx-tiers.md](../tdx-tiers.md)）
- Docker（選用，只有路線規劃與觀測需要）

## 啟動

設定後端環境變數：

```bash
cp backend/.env.example backend/.env
# 必填：LLM_BASE_URL、LLM_MODEL、TDX_CLIENT_ID、TDX_CLIENT_SECRET
# 後台寫入必填：ADMIN_TOKEN（高熵隨機值）
```

在 repo 根目錄執行：

```bash
process-compose up backend frontend
```

process-compose 會安裝依賴、啟動後端（`:8000`），待後端健康檢查通過後啟動前端。開啟 Vite 顯示的網址即可看到離站首頁。

不指定服務時，`process-compose up` 還會啟動兩個 Docker 服務：路線規劃用的 OTP 與觀測用的 SigNoz。沒有 Docker 或尚未建立 OTP graph 時，這兩個服務會顯示失敗，但不影響後端與前端。

process-compose 的本機設定（例如 `PC_PORT_NUM`）放在 `.pc_env`，已列入 `.gitignore`。

### 不使用 process-compose

分別在兩個終端機執行：

```bash
cd backend && uv sync && uv run uvicorn api:app --reload --port 8000
```

```bash
cd frontend && pnpm install && pnpm dev
```

## 常用設定

- Vite 預設把 `/api` 轉送到本機 `8000`；要改目標，設定 `frontend/.env` 的 `VITE_API_PROXY_TARGET`。前端若跨 origin 直接呼叫 API，用 `API_CORS_ORIGINS` 開放來源。
- 模型服務與後端在同一台機器時，`8000` 留給模型，後端改用其他 port（正式環境的後端使用 `127.0.0.1:8080`）。
- 使用 vLLM 時，tool calling 需要加上 `--enable-auto-tool-choice --tool-call-parser hermes --reasoning-parser qwen3`。

## 路線規劃（選用）

需要先建立 OTP graph，OTP 服務才能啟動，預設位址為 `http://localhost:8081`。步驟見 [backend/otp/README.md](../../backend/otp/README.md)，功能說明見 [route-planning.md](../route-planning.md)。

## 觀測（選用）

在 `backend/.env` 設定 `OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318`；未設定則不送出資料。UI 位址為 http://127.0.0.1:8085。查詢方式見 [observability.md](../observability.md)，SigNoz 的設定見 [backend/telemetry/README.md](../../backend/telemetry/README.md)。

## 確認服務正常

可以先呼叫以下 API：

- `GET /api/departures/here`：本站每條路線的狀態
- `GET /api/departures/routes/{route}/detail`：某條路線的完整站序
- `POST /api/route-plans`：body 為 `{"destination":{"lat":..,"lng":..},"departureTime":"2026-05-22T08:00:00+08:00"}`，回傳可在地圖上繪製的候選路線

不開前端、不使用語音，也能檢查完整的對話流程：

```bash
cd backend && uv run python e2e_test.py
```

這個腳本以實際的 LLM 與工具執行一段預先寫好的多輪對話，涵蓋固定回覆、各個工具與閒聊，並印出每一輪的輸入與回覆。腳本不做斷言，需要人工檢查。

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

測試 TDX 限流、上游失敗或快取過期時，不要呼叫真的 TDX。`TdxBusProvider` 可以注入假時鐘（`clock=`）與限速器，HTTP 則用 monkeypatch 換成假的回應，寫法參考 `backend/tests/providers/test_tdx_bus.py` 的 `_patch_http`。
