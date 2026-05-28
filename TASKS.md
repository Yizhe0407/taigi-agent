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

- [ ] 數位站務員腳本與狀態機：`tasks/station-attendant.md`
- [ ] 首頁 stale / offline / error 狀態：`tasks/departure-dashboard.md`
- [ ] Demo 主流程改寫：`tasks/demo-report.md`

## Next

- [ ] TTS 播報與 route card highlight 同步：`tasks/tts.md`
- [ ] ASR 短指令 intent set 與 test set：`tasks/asr.md`
- [ ] 使用者試用與量化評估設計：`tasks/evaluation.md`

## Later

- [ ] 路線規劃與 ebus 即時狀態整合評估：`tasks/route-planning.md`
- [ ] QR share：`tasks/qr-share.md`
- [ ] POI 與在地知識：`tasks/backlog.md`

## Workstreams

| 狀態 | Workstream | 任務 |
|------|------------|------|
| In progress | 離站決策首頁 | `tasks/departure-dashboard.md` |
| Not started | 數位站務員 | `tasks/station-attendant.md` |
| In progress | ASR 短指令 | `tasks/asr.md` |
| In progress | TTS 台語播報 | `tasks/tts.md` |
| In progress | 路線規劃 | `tasks/route-planning.md` |
| Deferred | QR share | `tasks/qr-share.md` |
| Not started | 評估與使用者試用 | `tasks/evaluation.md` |
| Not started | Demo 與報告 | `tasks/demo-report.md` |
| Backlog | 延伸項目 | `tasks/backlog.md` |

## 已完成基礎

- Agent harness：`AgentSession`、LLM client、tool dispatcher、prompt grounding、context compact、tool round limit、telemetry。
- ebus 工具：本站到站狀態、停靠路線、路線站序、stop-scoped route lookup、站名縮寫。
- 離站決策：資料模型、決策分類、`/api/departures/here`、route detail API、首頁 dashboard。
- 前端基礎：Vue、Tailwind、shadcn-vue、Lucide、Kiosk shell、PIP overlay、route planner full-page flow。
- 路線規劃：OTP graph、TDX stop index、coordinate planner、MapCN route view model、`POST /api/route-plans`；無班次錯誤顯示、地圖自動定位、站牌方向標示。
- 後台管理：`/admin` 站牌切換 UI；runtime `KioskConfig` singleton；`/api/admin/kiosk` GET/PUT、`/api/admin/stops`；不需重啟即可切換站牌與方向。
- 語音基礎：ASR proxy、前端錄音、TTS proxy、台語文字處理、分段播放。

## 文件

- 產品定位：`docs/product-positioning.md`
- 架構與目錄：`docs/architecture.md`
- 路線規劃邊界：`docs/route-planning.md`
- 可觀測性：`docs/observability.md`
