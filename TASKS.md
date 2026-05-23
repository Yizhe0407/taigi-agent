# 進度表

> 開始一個項目時改成 🏗️，完成後改成 ✅。

---

## Agent Harness 核心

| 狀態 | 項目 |
|------|------|
| ✅ | `AgentSession`：I/O 無關的 messages + tool-call loop |
| ✅ | CLI loop：外層 input loop 呼叫 `AgentSession` |
| ✅ | LLM client（OpenAI-compatible，env 設定） |
| ✅ | Tool dispatcher（TOOL_SCHEMAS + TOOL_HANDLERS） |
| ✅ | System prompt 組裝（`backend/agent/prompt.py`） |
| ✅ | Prompt grounding（正向約束防止 LLM 補充訓練資料、能力邊界明確化） |
| ✅ | 非思考模式（vLLM `extra_body={"chat_template_kwargs": {"enable_thinking": False}}`） |
| ✅ | Context 防守（LLM call 前 sliding-window trim + overflow retry） |
| ✅ | Token 計數（tiktoken cl100k_base token budget 取代 message count） |
| ✅ | Kiosk input enricher：路線號預取與 regex 防誤觸 |
| ✅ | 錯誤處理強化（LLM retry、context overflow retry、JSON parse 保護、tool call 上限） |
| ✅ | Harness tests：context exchange 完整性、tool error、tool round limit |
| ✅ | Context compact：transcript + 摘要 / 長 tool result 壓縮 |
| ✅ | 可觀測性：LLM latency、tool latency、tool error、retry、tool routing trace |

## 公車工具（雲林 ebus）

| 狀態 | 項目 |
|------|------|
| ✅ | `get_next_arrivals`：即時到站時間 |
| ✅ | `get_stop_arrival_statuses_here`：本站全部路線目前到站狀態 |
| ✅ | `get_route_stops`：本站停靠路線的站牌列表（從 estimate endpoint 重組） |
| ✅ | `get_routes_at_stop`：站名查停靠路線 |
| ✅ | 站名縮寫對照（`_ALIASES`） |
| ✅ | stop-based route cache：從 `/api/stop/route?stop_name=KIOSK_STOP` 解析 route id |
| ⬜ | `get_nearby_stops`：附近站牌（需 GPS 座標） |

## 路線規劃後端（OTP）

| 狀態 | 項目 |
|------|------|
| ✅ | OTP Docker 跑起來（GTFS + OSM graph build） |
| ✅ | `plan_route_to_coordinate`：Kiosk 出發到前端地圖選點座標的後端規劃核心 |
| ✅ | TDX Yunlin stop index：Kiosk 起點與 OTP GTFS 篩選資料邊界 |
| ✅ | MapCN route view model：OTP geometry 轉 `[lng, lat]` route options |
| ✅ | `POST /api/route-plans`：前端座標路線規劃 HTTP API |
| ⬜ | 跨縣市轉運引導（斗六火車站 / 雲林高鐵站 / 斗六轉運站） |
| ⬜ | route option 與 ebus 本站即時到站狀態整合評估 |

## 前端（Kiosk UI）

| 狀態 | 項目 |
|------|------|
| ✅ | Vue + Tailwind CSS + shadcn-vue + Lucide 前端骨架 |
| ✅ | MapCN Destination Picker：Kiosk 起點、地圖選點、拖曳、確認目的地 |
| ✅ | Route planning request / result view：候選路線、legs、MapCN `[lng, lat]` geometry |
| ✅ | 出發時間控制與無班次提示：現在出發 / 指定時間 |
| ✅ | Agent 對話 UI 與 route planning 流程入口 |
| ✅ | Kiosk / mobile layout polish 與主要 loading / empty / error states |

## 帶走路線（QR share）

> 解資訊持續性問題：Kiosk 是 query 介面，產出可帶走的 digital artifact。
> 路線結果上車 / 轉乘 / 走路時還能查。主要對應分眾的觀光客與陪同子女。

| 狀態 | 項目 |
|------|------|
| ⬜ | Route view model 設計 share serialization：純前端 base64 encode 進 URL hash，無需後端 storage，斷網仍可 render |
| ⬜ | Kiosk QR 顯示 overlay：路線結果頁加「帶走路線」按鈕，產生 QR（≥ 10 cm，加對準框與「掃描即可帶走」說明） |
| ⬜ | 手機端 `/share?plan=...` route：重用 `RoutePlannerPanel` + 地圖 render（read-only mode，無確認 / 重選按鈕） |
| ⬜ | Read-only RoutePlannerPanel 模式：抽出 prop 控制按鈕顯隱，避免 share 頁誤觸 Kiosk-only 流程 |
| ⬜ | Service worker offline cache：share 頁離線可用（鄉間 4G 斷續、車上隧道情境）。論文加分項，非 MVP |
| ⬜ | URL 長度量測：典型多 leg 路線 base64 後字元數，確認 QR error correction L 等級仍可掃，必要時切回後端 short link |
| ⬜ | 隱私揭露：share 連結含座標與行程，按鈕旁加「此連結可被分享者看到你的路線」一行說明 |
| ⬜ | 量化指標（論文用）：受試者掃 QR 後在轉乘站行程完成率 vs 控制組（記憶） |

風險與決策點：
- **URL hash vs 後端 short link**：先 hash（無狀態、永久、純前端），實測 QR 掃描率不夠再退 short link
- **手機與 Kiosk 共用 Vue app vs 獨立 mobile build**：先共用、加 share-mode flag；若 bundle 太大再切
- **長輩單獨使用情境**：本流程預設不服務獨自長輩，分流由「使用者分眾」段落涵蓋

## POI 與在地知識

| 狀態 | 項目 |
|------|------|
| ⬜ | 雲科 + 斗六周邊 POI 資料集（餐廳 / 景點 / 醫療） |
| ⬜ | `search_poi`：附近店家 / 景點查詢 |
| ⬜ | 校園知識（雲科系所 / 行政 / 活動） |

## 語音接入

| 狀態 | 項目 |
|------|------|
| ⬜ | ASR 接入（打字 → 語音輸入）|
| ⬜ | TTS 接入（文字輸出 → 語音輸出，Piper 台語模型）|
| ⬜ | LiveKit 整合（WebRTC 傳輸）|
| ⬜ | ASR 地名 hotword injection（雲林地名字典）|
| ⬜ | TTS text normalization（公車代號 / 英文 / 數字）|

## 量化評估（論文用）

| 狀態 | 項目 |
|------|------|
| ⬜ | Tool routing 正確率（intent → 正確 tool 命中率）|
| ⬜ | First-audio latency（接語音後，p50 / p95）|
| ⬜ | ASR 地名辨識率（自建 test set ≥ 100 句）|
| ⬜ | TTS 自然度 MOS 評分（N ≥ 10 主觀評分）|

## 使用者試用

| 狀態 | 項目 |
|------|------|
| ⬜ | 訪談腳本 + 同意書 |
| ⬜ | 學生試用（N ≥ 10）|
| ⬜ | 長輩試用（N ≥ 5）|
| ⬜ | SUS 問卷整理 |

## Demo 與報告

| 狀態 | 項目 |
|------|------|
| ⬜ | Demo 腳本（3 / 5 / 10 分鐘三版）|
| ⬜ | 架構圖 / 時序圖（Mermaid）|
| ⬜ | 書面報告 |
| ⬜ | Demo 預演 ≥ 3 次 |
