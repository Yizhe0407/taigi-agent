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
| ✅ | System prompt 組裝（`agent/prompt.py`） |
| ✅ | Prompt grounding（正向約束防止 LLM 補充訓練資料、能力邊界明確化） |
| ✅ | 非思考模式（vLLM `extra_body={"chat_template_kwargs": {"enable_thinking": False}}`） |
| ✅ | Context 防守（LLM call 前 sliding-window trim + overflow retry） |
| ✅ | Token 計數（tiktoken cl100k_base token budget 取代 message count） |
| ✅ | Kiosk input enricher：路線號預取與 regex 防誤觸 |
| ✅ | 錯誤處理強化（LLM retry、context overflow retry、JSON parse 保護、tool call 上限） |
| ✅ | Harness tests：context exchange 完整性、tool error、tool round limit |
| ⬜ | Context compact：transcript + 摘要 / 長 tool result 壓縮 |
| ⬜ | 可觀測性：LLM latency、tool latency、tool error、retry、tool routing trace |

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
