# CLAUDE.md

雲林固定站牌台語友善離站決策系統。Agent harness 架構：one loop + tools + prompt = agent。

架構全貌見 `docs/architecture.md`，逐模組職責見 `docs/architecture-reference.md`（唯一真相源）；產品定位見 `docs/product-positioning.md`；完成功能與未做項目見 `docs/changelog.md`；部署見 `docs/deployment/`。

## 開場協議

1. 專案已進入維護期，無進行中任務；要重啟開發時，先讀 `docs/changelog.md` 的「## 未完成」與 `docs/archive/` 的設計紀錄。
2. 完成任一修改 → 依下方文件規則更新對應文件。
3. 派 subagent 前 → 照 `docs/playbook/model-dispatch.md` 選模型、用 `docs/playbook/prompts.md` 範本。

## Playbook 索引（按需讀取，勿全載）

- `docs/playbook/model-dispatch.md` — 模型調度：誰派誰、升降級、驗證不自驗。**每次派工前讀。**
- `docs/playbook/prompts.md` — 交辦 prompt 範本（搜尋/實作/重構/研究/審查/read-back）。
- `docs/playbook/judgment.md` — 判斷 rubric：何時升級、何時算完成、何時問使用者、方向錯的訊號。**卡住或準備說「完成」前讀。**
- `docs/playbook/maintenance.md` — 哪些檔可自行改、踩雷教訓寫哪裡。**踩雷後讀。**
- `docs/playbook/lessons.md` — 踩雷教訓帳本。
- `docs/archive/` — 已完成計劃與一次性診斷（pipecat、PiP UX、diagnosis、letter-to-future-sessions），僅供參考。

## 常用指令

```bash
cd backend
uv sync
uv run uvicorn api:app --reload --port 8000
uv run pytest
uv run ruff check .

cd frontend
pnpm install
pnpm dev
```

## 固定約束

- Python 套件管理用 `uv`，不要改用 pip / poetry / conda。
- `backend/agent/session.py` 只處理 messages、LLM call、tool dispatch、context recovery；公車 prefetch、provider 規則與領域邏輯留在 `backend/tools/`、`backend/services/`、`backend/providers/`。
- Tool handler 必須回傳 `str`；`session.py` 會把 tool result 直接送回 LLM。
- 修改 code 後要說明「做了什麼、為什麼這樣寫、可能的坑」。
- 修改 code 後自行判斷文件更新：使用方式改變更新 `README.md`；功能/限制改變更新 `docs/changelog.md`；架構/邊界改變更新 `docs/architecture.md` 或相關 `docs/`。
- 每完成一個 phase 就 commit，不要讓 working tree 累積跨任務改動。

## 新增工具流程

1. 外部資料源放 `backend/providers/`：定義或重用 Protocol，再加具體實作。
2. 分類、決策、結構化 dataclass 放 `backend/services/`，透過 Protocol 呼叫 provider。
3. Agent 可見的字串 facade 放 `backend/tools/`，或由 service 提供 `render_*`。
4. 在 `backend/agent/tools.py` 加 import、`TOOL_SCHEMAS` 與 `TOOL_HANDLERS`。
5. 不要在 `AgentSession` 加公車專屬分支；若需輸入預取，從入口注入 `input_enricher`。

## 重要 Gotchas

- Route lookup 是 stop-scoped：只查 `KIOSK_STOP` 停靠路線，避免同名 route 歧義。
- Kiosk 方向設定語意：admin 設「去程」或「回程」→ 直接過濾，不 auto-detect；設「去回程都有」(go_back=None) → `_is_terminal_direction()` 自動過濾終點到站方向，循環路線不過濾。
- **方向編碼**：TDX Direction 0=去程、1=回程（非舊 ebus 的 1/2）。`kiosk_config.go_back`、`iter_scoped_stop_etas` 的 `go_back` 參數、API response `goBack` 全部用 0/1。
- **TDX provider**：`providers/tdx_bus.py` 同時查 `City/YunlinCounty` 與 `InterCity` 兩個 endpoint 並合併。`load_route_info` 從 `StopOfRoute` Stops 末站推導 `go_dest`/`back_dest`，並保留完整站序到 `RouteInfo.outbound_stops`/`inbound_stops`。
- **TDX 限速器**：所有 TDX API 請求（公車＋單車，同一把金鑰）都要先 `tdx_rate_limiter(client_id).acquire()`，額度由 `TDX_RATE_LIMIT` 設定（預設 `5/min` 基礎會員）。背景呼叫只能用約 60% 額度，其餘保留給有預算的互動呼叫；429 用 `penalize()` 暫停整把金鑰，不要在 provider 內自己 sleep。
- **上游時間預算**：工具派發用 `upstream_deadline(TOOL_UPSTREAM_BUDGET_SECONDS)` 包住每次工具呼叫；會阻塞的上游程式碼（TDX `_get` 的 429 退避、`TtlCache` / StopOfRoute 的鎖等待）要看 `remaining_budget()`，不得等超過，改丟 `UpstreamBudgetExceeded` 讓快取供舊資料。背景工作沒有預算，照常等 Retry-After。
- **路線拓撲 vs 即時資料**：「這條路線之後會不會到 X」是靜態問題，用 `RouteInfo` 站序回答（`rows._iter_route_downstream`）；`fetch_route_estimate` 只用來取即時 ETA，不要再為了判斷路線形狀逐條抓。
- **到站資料欄位**（`providers/bus.py`，provider-neutral）：`StopArrival` — `route_name`、`direction`(0/1)、`status`(`StopStatus`)、`eta_seconds`(int|None)、`sequence`、`scheduled_time`、`vehicle_id`。`RouteStopEstimate` 多了 `stop_name`，`route_name` 可為 None。`route_id` 整個 service/API 層是 `str`。
- **TDX StopStatus**：0=正常、1=未發車、2=交管不停（`iter_scoped_stop_etas` 靜默過濾）、3=末班已過、4=今日未營運。無 `ComeTime` 等效，`scheduled_time` 永遠 None。
- **TDX 認證**：`TDX_CLIENT_ID` / `TDX_CLIENT_SECRET` 放 `.env`；token 用 OAuth2 client_credentials 自動取得並快取。
- 站名/路線沒有人工縮寫對照表；ASR 聽錯救援由工具自己做：查無時 renderer 用音近排序（`departures/fuzzy_match`）挑第一名重查一次，把真實狀態連同確認句前綴回給 LLM（`renderers._rescue_or`）；LLM 只改寫成「你是要問 X 嗎？X…」，不再呼叫工具。prompt 端規則在 `agent/prompt.py`【聽錯救援】。
- 截斷 messages 必須以 tool-call 輪次為單位，不能讓 `tool_call_id` 失去對應 tool result。
- Tool round limit 達上限時，不可先把新的 assistant `tool_calls` append 進 history 再跳出。
- `.agent_state/` 是 runtime state（`sessions.db`、`kiosk_config.json`），已由 `.gitignore` 排除；測試要把寫入路徑指向 `tmp_path`（如 `ChatSessionStore(tmp_path / "sessions.db")`）。
- `load_dotenv()` 必須在依賴 env 的 import 之前。
- vLLM tool calling 需要 `--enable-auto-tool-choice --tool-call-parser hermes --reasoning-parser qwen3`。
- vLLM 非思考模式格式是 `{"chat_template_kwargs": {"enable_thinking": False}}`。
- Telemetry content-level 觀測預設開啟（user input、prompt、LLM 回應、tool result、ASR/TTS 文字掛在 span attributes，經 `set_content()` 截斷）；設 `TELEMETRY_CAPTURE_CONTENT=false` 關閉。原始音訊 bytes 不收，只記大小。
- 永不讀取：`uv.lock`、`pnpm-lock.yaml`、`frontend/.vite/`、`backend/data/`、`backend/otp/data/`（大檔，讀了只會漏 token）。

## Commit 規範

Conventional Commits：
- `feat(tools):` 新增工具
- `fix(tools):` 修 bug
- `feat(agent):` 修改 harness 核心
- `docs:` 文件
