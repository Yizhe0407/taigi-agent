# CLAUDE.md

雲林固定站牌台語友善離站決策系統。Agent harness 架構：one loop + tools + prompt = agent。

詳細架構與目錄說明見 `docs/architecture.md`；產品定位見 `docs/product-positioning.md`；進度見 `TASKS.md`。

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
- 修改 code 後自行判斷文件更新：使用方式改變更新 `README.md`；功能進度改變更新 `TASKS.md`；架構/邊界改變更新 `docs/architecture.md` 或相關 `docs/`。

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
- **TDX provider**：`providers/tdx_bus.py` 同時查 `City/YunlinCounty` 與 `InterCity` 兩個 endpoint 並合併。`load_route_info` 從 `StopOfRoute` Stops 末站推導 `go_dest`/`back_dest`。
- **TDX 欄位**：ETA rows — `sub_route_name`(str)、`direction`(0/1)、`stop_status`(0-4)、`estimate_seconds`(int|None)。route estimate rows 多加 `stop_name`、`stop_sequence`。`route_id` 整個 service/API 層是 `str`。
- **TDX StopStatus**：0=正常、1=未發車、2=交管不停（`iter_scoped_stop_etas` 靜默過濾）、3=末班已過、4=今日未營運。無 `ComeTime` 等效，`scheduled_time` 永遠 None。
- **TDX 認證**：`TDX_CLIENT_ID` / `TDX_CLIENT_SECRET` 放 `.env`；token 用 OAuth2 client_credentials 自動取得並快取。
- 站名縮寫要人工處理；縮寫對照在 `backend/tools/kiosk_bus.py` 的 `_ALIASES`。
- 截斷 messages 必須以 tool-call 輪次為單位，不能讓 `tool_call_id` 失去對應 tool result。
- Tool round limit 達上限時，不可先把新的 assistant `tool_calls` append 進 history 再跳出。
- `.agent_state/` 是 runtime state（`sessions.db`、`kiosk_config.json`），已由 `.gitignore` 排除；測試要把寫入路徑指向 `tmp_path`（如 `ChatSessionStore(tmp_path / "sessions.db")`）。
- `load_dotenv()` 必須在依賴 env 的 import 之前。
- vLLM tool calling 需要 `--enable-auto-tool-choice --tool-call-parser hermes --reasoning-parser qwen3`。
- vLLM 非思考模式格式是 `{"chat_template_kwargs": {"enable_thinking": False}}`。
- Telemetry content-level 觀測預設開啟（user input、prompt、LLM 回應、tool result、ASR/TTS 文字掛在 span attributes，經 `set_content()` 截斷）；設 `TELEMETRY_CAPTURE_CONTENT=false` 關閉。原始音訊 bytes 不收，只記大小。

## Commit 規範

Conventional Commits：
- `feat(tools):` 新增工具
- `fix(tools):` 修 bug
- `feat(agent):` 修改 harness 核心
- `docs:` 文件
