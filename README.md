# taigi-bus-agent

雲林公車台語語音助理（大學專題）。

以 **agent harness** 為架構核心，讓使用者用台語詢問雲林縣公車資訊。
目前為打字 CLI 模式，後期接語音（ASR + TTS）。

## 架構概念

```
使用者輸入
    ↓
AgentSession（harness 核心）
    ├─ LLM 決定要呼叫哪個工具
    ├─ 執行工具（雲林公車動態資料）
    └─ LLM 組成自然語言回答
    ↓
使用者看到答案
```

參考架構：[learn-claude-code](https://github.com/shareAI-lab/learn-claude-code)
產品定位詳見 `docs/product-positioning.md`，架構與目錄細節見 `docs/architecture.md`。

CLI 目前只是 I/O 層；`AgentSession` 保持輸入輸出為文字，後續 ASR/TTS
可沿用同一個 session runtime。Kiosk 路線號預取由入口注入，不寫死在 harness 核心。
Context 過長時會把 compact 前 transcript 與完整長工具輸出保存到 `.agent_state/`，
active context 只保留摘要或預覽。

## 場域

專題範圍是雲林縣內的站牌 Kiosk。每台 Kiosk 用 `KIOSK_STOP` 指定部署站牌，
系統只回答該站牌可查到的到站與路線資訊。

資料來源使用雲林公車動態系統後端資料介面。這能覆蓋專題需要的站牌查詢，
但該介面不是本專題可控制的公開契約；若介面變更，調整點集中在
`backend/providers/yunlin_ebus.py`。

## 使用者分眾

公車 Kiosk 不是單一產品。同一套 harness + 同份雲林資料，依部署站牌服務不同人群，
聊天到站與地圖路線規劃的權重也不同：

| Kiosk 部署 | 主要使用者 | 高頻需求 | UI 重心 |
|------------|------------|----------|---------|
| 雲科大、虎尾科大 | 學生、教職員、接送家長 | 到站對話 + 校外景點 / 車站路線 | 兩者並重 |
| 虎尾火車站、高鐵雲林站 | 觀光客轉乘 | 景點 POI 路線規劃 | 路線規劃 + 高頻 POI 為主 |
| 鄉鎮小站（口湖、四湖、台西…） | 在地長輩 | 「現在還有車嗎」、「末班開了沒」 | 到站對話為主，路線規劃隱藏或縮成 3 個 POI |

設計依據：

- 長輩多半已經知道搭哪條（看過時刻表牌、坐過很多次），真實痛點是
  「這班還來嗎」、「末班會不會開走」、「現在幾分到」，這些由聊天到站工具解決
- 「未知目的地座標」是工程錯覺。公車目的地本質上是雲林 stop catalog 的有限集，
  不是連續地圖座標
- 地圖 pan / zoom / 拖圖釘對 60+ 使用者認知負擔高。路線規劃實際上是給
  陪同子女、外籍看護、觀光客用，承認分眾比假裝單一 UI 萬用更誠實
- 技術面幾乎不變：`KIOSK_STOP` 已是場域變數，後續若要分流 UI，加一個
  `KIOSK_AUDIENCE=elderly | tourist | mixed` 控制首頁 tab 預設與功能顯隱即可，
  後端 API 不動

## 前置需求

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- OpenAI-compatible LLM API（Ollama 或 vLLM）

### vLLM 啟動參數（若使用 vLLM）

tool calling 與非思考模式需要額外參數：

```bash
vllm serve Qwen/Qwen3-4B-Instruct-2507 \
  --enable-auto-tool-choice --tool-call-parser hermes \
  --reasoning-parser qwen3
```

## 安裝與執行

repo root 現在只放跨前後端文件與 app 目錄；Python agent、HTTP API、OTP
與後端測試都在 `backend/`，Vue app 在 `frontend/`。

```bash
# 1. 安裝依賴
cd backend
uv sync

# 2. 設定環境變數
cp .env.example .env
# 必填：LLM_BASE_URL、LLM_MODEL
# Kiosk 設定：KIOSK_STOP（這台機器在哪個站牌，預設「雲林科技大學」）
# 選填：KIOSK_DIRECTION=去程 或 回程（不填 = 顯示兩個方向）

# 3. 啟動
uv run python main.py
```

Kiosk 前端走獨立 HTTP API：

```bash
cd backend
uv run uvicorn api:app --reload --port 8000
```

離站決策首頁使用：

- `GET /api/departures/here`：本站路線、方向、到站 / 未發車 / 末班決策
- `GET /api/departures/routes/{route}/detail`：本站停靠路線的真實站序詳情

路線規劃需要本機 OTP graph、雲林 stop index 與 service。GTFS / stop index
更新、graph build 與 Docker 啟動步驟見 `backend/otp/README.md`；預設 OTP
service 位置是 `http://localhost:8081`。
OTP / ebus / MapCN 的分工與資料風險見 `docs/route-planning.md`。

`POST /api/route-plans` 只收前端已確認的目的地座標與可選出發時間：

```json
{
  "destination": { "lat": 23.717831598831527, "lng": 120.53840824484192 },
  "departureTime": "2026-05-22T08:00:00+08:00"
}
```

回應含 Kiosk 起點、目的地與可給 MapCN `MapRoute` 的 `[lng, lat]`
候選路徑。若前端 dev server 不走 Vite `/api` proxy，而是跨 origin 直接打
API，再用 `API_CORS_ORIGINS` 明確開放前端來源。

Vue Kiosk 前端放在 `frontend/`。目前第一個畫面是本站離站決策 dashboard；
點「規劃路線」後進入 MapCN Vue 地圖選點流程：

```bash
cd frontend
pnpm install
pnpm dev
```

首頁顯示固定 Kiosk 站牌的可搭、未發車與末班狀態；點路線可查看後端
`/api/departures/routes/{route}/detail` 回傳的真實站序。路線規劃頁固定顯示
雲林科技大學 Kiosk 起點，可點地圖或拖曳圖釘確認目的地；確認後會呼叫
route planning API，在地圖畫出目前選取的候選路線，面板顯示轉乘、時間與
legs。Vite dev server 預設把 `/api` 轉送到本機 `8000` port；需要不同 API
目標時再設定 `frontend/.env` 的 `VITE_API_PROXY_TARGET`。

### 路線色策略

首頁 Hero 大卡、右側路線列表、路線詳情與路線規劃共用同一套路線色流程：

- 底層在 `frontend/src/features/departures/kiosk-data.ts`
- 畫面層入口在 `frontend/src/features/departures/composables/useRouteColors.ts`

目前演算法是：

1. 先把 `routeCode` 正規化並做 deterministic hash
2. 對每條路線產生固定候選色順序
3. 針對同一畫面實際出現的 route set 做 greedy `max-min` assignment
4. 優先拉開同屏路線間的感知距離，必要時才重用顏色

目前候選色池是 24 色，刻意維持和 kiosk 主題一致：明亮、乾淨、少量活潑，
但不使用過暗、過灰或過螢光的色。

這套策略目前判定為**可正式使用**，但邊界要講清楚：

- 適用情境：Kiosk 首頁、路線詳情、路線規劃這種同屏約 6 到 12 條路線的 UI
- 設計目標：提升同屏辨識度，不是替全雲林所有路線建立永久唯一色
- 已知限制：若一次拿上百條 route code 一起測，因為色池有限，重複仍然必然發生

也就是說，這不是「全資料集 collision-free」演算法，而是「符合目前產品畫面密度、
視覺主題與可讀性需求」的實務版本。

## 可觀測性

目前預設觀測後端選 SigNoz。Agent runtime 只依賴 OpenTelemetry，透過
OTLP/HTTP 把 traces 與 metrics 送到 SigNoz；未設定 endpoint 時不會送出
telemetry。若 self-host SigNoz 跑在本機，在 `backend/.env` 設定：
詳細 spans、metrics 與不收集內容見 `docs/observability.md`。

```bash
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
OTEL_SERVICE_NAME=taigi-bus-agent
```

目前 spans 包含 `agent.turn`、`agent.llm.call`、`agent.tool.routing`、
`agent.tool.call`。metrics 包含 `agent.llm.duration`、`agent.llm.retry`、
`agent.tool.duration`、`agent.tool.error`。harness 預設不把 user input、
prompt 或 tool result 放進 span attributes。

## 目前支援功能

系統為 Kiosk 模式，部署在固定站牌，查詢「這站」的到站資訊。

| 問法 | 工具 | 資料來源 |
|------|------|----------|
| 「201 幾分鐘到」 | `get_arrivals_here` | ebus.yunlin.gov.tw |
| 「目前還有哪些車」 | `get_stop_arrival_statuses_here` | ebus.yunlin.gov.tw |
| 「7126 下一班幾分鐘到」 | `get_arrivals_here` | ebus.yunlin.gov.tw |
| 「201 停哪些站」 | `get_route_stops` | ebus（從到站資料重組） |
| 「7126 停哪些站」 | `get_route_stops` | ebus（限本站停靠路線） |
| 「這站有哪些路線」 | `get_routes_at_stop` | ebus.yunlin.gov.tw |

路線規劃不是聊天文字 tool。產品主流程是前端地圖讓使用者選目的地座標，
後端 `plan_route_to_coordinate(latitude, longitude)` 從 Kiosk 起點做 OTP
規劃，再用 route view model 給 MapCN 畫候選路徑；聊天中問「怎麼去某地」時，
助理只引導使用地圖選點。聊天工具不支援：
完整一日時刻表、文字目的地路線規劃、站間即時行駛時間。

## 舊專案

架構重寫前的版本（含 LiveKit 語音、Admin 後台）：`/Users/yizhe/Developer/taigi-flow`（唯讀參考）
