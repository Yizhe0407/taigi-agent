# 進度表

> 開始一個項目時改成 🏗️，完成後改成 ✅。

---

## Agent Harness 核心

| 狀態 | 項目 |
|------|------|
| ✅ | Main loop（外層 input loop + 內層 tool-call loop） |
| ✅ | LLM client（OpenAI-compatible，env 設定） |
| ✅ | Tool dispatcher（TOOL_SCHEMAS + TOOL_HANDLERS） |
| ✅ | System prompt 組裝（`agent/prompt.py`） |
| ✅ | Prompt grounding（正向約束防止 LLM 補充訓練資料、能力邊界明確化） |
| ✅ | 非思考模式（vLLM `extra_body={"chat_template_kwargs": {"enable_thinking": False}}`） |
| ✅ | Context 截斷（sliding window，`agent/context.py`） |
| ✅ | Token 計數（tiktoken cl100k_base token budget 取代 message count） |
| ✅ | `_prefetch` regex 防誤觸（negative lookahead 排除大樓 / 出口 / 樓層等） |
| ⬜ | 錯誤處理強化（tool 失敗 retry / graceful degradation） |

## 公車工具（TDX API）

| 狀態 | 項目 |
|------|------|
| ✅ | TDX OAuth token 取得 + 快取 |
| ✅ | `get_next_arrivals`：即時到站時間 |
| ✅ | `get_schedule`：今日時刻表 |
| ✅ | `get_route_stops`：路線站牌列表（TDX 路線用 StopOfRoute；ebus 路線從 estimate endpoint 重組） |
| ✅ | 站名縮寫對照（`_ALIASES`） |
| ⬜ | `get_nearby_stops`：附近站牌（需 GPS 座標） |
| ⬜ | **重構考慮**：若 `get_schedule` 移除，yunlin_ebus 的 provider filter 可改成 stop-based lookup（從 `/api/stop/route?stop_name=KIOSK_STOP` 建立 route cache，天然去歧義，不需 `_LOCAL_PROVIDER_IDS`） |
| ✅ | 雲林縣府自管路線支援（201 / Y01 等）：ebus.yunlin.gov.tw，`_LOCAL_PROVIDER_IDS` 過濾確保是雲林本地路線 |

## 路線規劃（OTP）

| 狀態 | 項目 |
|------|------|
| ⬜ | OTP Docker 跑起來（GTFS + OSM graph build） |
| ⬜ | `plan_route`：從 A 到 B 的完整轉乘建議 |
| ⬜ | 跨縣市轉運引導（斗六火車站 / 雲林高鐵站 / 斗六轉運站） |

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
