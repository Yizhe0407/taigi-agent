# 路線規劃

## 目標

路線規劃是 secondary flow，用來服務展示、學生、觀光客或陪同者。聊天 Agent 不做自由文字目的地猜測；前端確認座標後才呼叫 API。

## 狀態

Mostly done。OTP graph、coordinate planner、MapCN view model 與前端流程已完成；剩下轉運引導與 ebus 即時狀態整合評估。

## 已完成

- [x] OTP Docker graph build 與 serve 流程
- [x] TDX Yunlin stop index
- [x] `plan_route_to_coordinate`
- [x] MapCN route view model：OTP geometry -> `[lng, lat]`
- [x] `POST /api/route-plans`
- [x] MapCN Destination Picker：起點、選點、拖曳、確認目的地
- [x] Route planning request / result view
- [x] 出發時間控制與無班次提示
- [x] 路線規劃頁改為全頁 secondary flow，不再用首頁 tab 切換
- [x] 無路線結果時顯示明確錯誤卡片（原本按鈕蓋住錯誤訊息）
- [x] 地圖標記顯示當前站牌名稱與方向（去程／回程）
- [x] 進入路線規劃後地圖自動定位至當前站牌（API 回傳後 flyTo）

## 待辦

- [ ] 跨縣市轉運引導：斗六火車站 / 雲林高鐵站 / 斗六轉運站
- [ ] route option 與 ebus 本站即時到站狀態整合評估
- [ ] 資料風險驗證：`7000D` / `7000B`、route naming mapping、站名 mapping

## 驗收

- [x] 找不到路線有明確錯誤提示（無班次黃色卡片 / 一般錯誤紅色卡片）
- [x] 座標格式錯誤、OTP timeout 都有明確錯誤
- [x] 結果頁可畫出候選路徑，且文字摘要和路線 geometry 對得上
- [x] Agent 對 route planning 意圖只引導前端選點，不猜目的地

## 相關文件

- `docs/route-planning.md`
- `backend/otp/README.md`
