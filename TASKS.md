# 任務總覽

本檔只放目前優先順序與任務索引。具體工作拆在 `tasks/`，穩定決策放在 `docs/`。

狀態標記：
- `Done`：已完成
- `In progress`：正在推進或核心已完成但仍有收尾
- `Deferred`：刻意延後
- `Not started`：尚未開始

## 產品主軸

作品名稱：**台語友善固定站牌離站決策系統**

副標：**為低數位門檻使用者轉譯即時公車資訊**

第一優先是固定站牌離站決策：把即時到站、方向、未發車與末班駛離轉成「現在可不可以搭、要不要等、哪些方向已經沒車」的可行動資訊。

數位虛擬人定位為「數位站務員」：負責台語播報、引導與畫面重點同步，不作為自由聊天主體。地圖路線規劃、QR share、任意目的地查詢降為後續或展示輔助。

## Now

- [ ] Demo 腳本與報告素材：`tasks/demo-report.md`
- [ ] 站務員視覺狀態機：`tasks/station-attendant.md`（blocked，等組員 3D 模型）

## Later

- [ ] 使用者試用與量化評估：`tasks/evaluation.md`（報告素材）
- [ ] POI 與在地知識：`tasks/backlog.md`

## Workstreams

| 狀態 | Workstream | 任務 |
|------|------------|------|
| Done | 離站決策首頁 | `tasks/departure-dashboard.md` |
| Done | TTS 台語播報 | `tasks/tts.md` |
| Done | ASR 短指令 | `tasks/asr.md` |
| Done | 路線規劃 | `tasks/route-planning.md` |
| Done | PiP 語音對話 UX | `tasks/pip-voice-ux.md` |
| Blocked | 數位站務員 | `tasks/station-attendant.md`（等組員模型）|
| Not started | Demo 與報告 | `tasks/demo-report.md` |
| Not started | 評估與使用者試用 | `tasks/evaluation.md` |
| Backlog | 延伸項目 | `tasks/backlog.md` |

## 已完成基礎

- Agent harness：`AgentSession`、`IntentRouter`（Python regex 意圖分類）、`ConvState`（顯式對話狀態）、LLM client、tool dispatcher、prompt grounding、context cap（MAX_EXCHANGES=5）、tool round limit、telemetry。
- ebus 工具：本站到站狀態、停靠路線、路線站序、stop-scoped route lookup、站名縮寫。
- 離站決策：資料模型、決策分類、`/api/departures/here`、route detail API、首頁 dashboard。
- 前端基礎：Vue、Tailwind、shadcn-vue、Lucide、Kiosk shell、PIP overlay、route planner full-page flow。
- 路線規劃：OTP graph、TDX stop index、coordinate planner、MapCN route view model、`POST /api/route-plans`；無班次錯誤顯示、地圖自動定位、站牌方向標示。
- 後台管理：`/admin` 站牌切換 UI；runtime `KioskConfig` singleton；`/api/admin/kiosk` GET/PUT、`/api/admin/stops`；不需重啟即可切換站牌與方向。
- **公車資料來源雙 provider 架構**：`providers/hybrid.py` 為唯一線上 `BusProvider` runtime；路線目錄（`load_route_info`、`fetch_routes_at_stop`）由 TDX 提供，ETA（`fetch_eta_at_stop`、`fetch_route_estimate`）由 ebus.yunlin.gov.tw 主力，ebus 空值時 fallback 至 TDX intercity。TDX Direction 0/1，`route_id` 全層為 SubRouteName string，`_classify_stop` 讀 `stop_status`/`estimate_seconds`。
- 語音基礎：ASR proxy、前端錄音、TTS proxy、台語文字處理、分段播放；ASR 錯誤訊息不外洩原始 Python exception。
- 串流回覆：`AgentSession.respond_stream` 逐句輸出 → 語音逐句 TTS（首音不等完整回覆）、chat SSE 逐字上屏、departures SSE 隨 ETA warmup tick 推播（取代輪詢相位差）。
- 方向過濾 auto-detect：`_is_terminal_direction()` 自動過濾終點到站方向；admin 設定「去回程都有」時啟動，設定單方向時直接照設定過濾；循環路線不過濾。
- **ASR 聽錯救援強化**（2026-07-13 實測 20 案：完全成功 20%→75%、失敗 40%→0、確認句時間幻覺歸零）：站名 fuzzy 加 pypinyin 無聲調拼音維度（救零字重疊同音錯：刺同→莿桐）；數字路線號改加權編輯距離排序；四支查詢工具查無時 renderer 自動用 top 候選重查、回覆掛「你問的X查無，最接近的是Y」前綴——真實狀態由工具給、小模型只改寫成確認句，不再依賴 4B 自行二次呼叫工具。

## 文件

- 產品定位：`docs/product-positioning.md`
- 架構與目錄：`docs/architecture.md`
- 路線規劃邊界：`docs/route-planning.md`
- 可觀測性：`docs/observability.md`
