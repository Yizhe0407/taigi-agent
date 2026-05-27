# 離站決策首頁

## 目標

固定站牌首頁直接回答「現在能不能搭、要不要等、哪些方向已經沒車」，不要求使用者先開 chat 或地圖。

## 狀態

In progress。主要資料模型、API 與首頁 UI 已完成；剩下 stale/offline/error 與單一路線查詢細節。

## 已完成

- [x] `StopDepartureSnapshot` / `DepartureRouteStatus` / `DepartureDecision` 資料模型
- [x] 決策分類：`arriving_soon` / `can_wait` / `long_wait` / `not_departed` / `last_departed` / `unknown`
- [x] `GET /api/departures/here`
- [x] `GET /api/departures/routes/{route}/detail`
- [x] structured departure snapshot：把 ebus 文字狀態轉成前端可渲染資料
- [x] 第一畫面改為離站決策 dashboard
- [x] route cards：車號、方向、等待時間、決策 badge、狀態說明
- [x] 雙欄首頁：Hero 大字卡 + 路線列表
- [x] 路線色 badge 與 route detail timeline
- [x] 移除沒有可靠來源的一日班表假資料
- [x] 前端自動刷新，不打斷使用者正在看的內容

## 待辦

- [x] 資料更新時間與 stale 判斷：最後更新、逾時、查詢失敗
- [x] 低干擾 loading / stale / offline / error 狀態
- [ ] 單一路線 detail API 與播報摘要共用資料
- [ ] 評估是否需要 `get_nearby_stops`，目前需 GPS 座標，非 MVP

## 驗收

- [ ] 首頁無需使用者輸入即可顯示本站可搭、未發車、末班已過狀態
- [ ] API 失敗或資料逾時時，畫面不誤導使用者
- [ ] 點選路線可看到後端真實站序與狀態

## 相關文件

- `docs/product-positioning.md`
- `docs/architecture.md`
