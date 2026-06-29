# 離站決策首頁

## 目標

固定站牌首頁直接回答「現在能不能搭、要不要等、哪些方向已經沒車」，不要求使用者先開 chat 或地圖。

## 狀態

大致完成。TDX migration 後清除了 `scheduledTime` 死邏輯；剩下單一路線播報共用資料為 optional。

## 已完成

- [x] `StopDepartureSnapshot` / `DepartureRouteStatus` / `DepartureDecision` 資料模型
- [x] 決策分類：`arriving_soon` / `can_wait` / `long_wait` / `not_departed` / `last_departed` / `unknown`
- [x] `GET /api/departures/here`
- [x] `GET /api/departures/routes/{route}/detail`
- [x] 第一畫面改為離站決策 dashboard
- [x] route cards：車號、方向、等待時間、決策 badge、狀態說明
- [x] 雙欄首頁：Hero 大字卡 + 路線列表
- [x] 路線色 badge 與 route detail timeline
- [x] 前端自動刷新，不打斷使用者正在看的內容
- [x] 資料更新時間顯示（lastUpdatedText）
- [x] 低干擾 background error banner（refetch 失敗但快取仍顯示）
- [x] 首次載入 spinner / 全屏 error（無快取時）
- [x] 今日已收班 NoServiceHeroCard（TDX 無 scheduledTime，移除 tomorrowFirstTime 死邏輯）

## 待辦

- [ ] 單一路線 detail API 與播報摘要共用資料（optional）
- [ ] `get_nearby_stops`（需 GPS 座標，non-MVP）

## 驗收

- [x] 首頁無需使用者輸入即可顯示本站可搭、未發車、末班已過狀態
- [x] API 失敗或資料逾時時，畫面不誤導使用者
- [x] 點選路線可看到後端真實站序與狀態

## 相關文件

- `docs/product-positioning.md`
- `docs/architecture.md`
