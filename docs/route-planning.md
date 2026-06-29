# 路線規劃邊界

本專案的核心是固定站牌離站決策；路線規劃是第二層流程，用來支援展示、觀光客、陪同者或需要地圖選點的使用者。聊天 Agent 不直接做自由文字目的地規劃。

## 產品邊界

- 起點固定為 `KIOSK_STOP` 對應的站牌或座標。
- 目的地由前端地圖選點確認後傳入，不讓自由文字 geocoding 成為主路徑。
- 前端呼叫 `POST /api/route-plans`，後端回傳 MapCN 可渲染的 route view model。
- Agent 收到「怎麼去某地」時，只引導使用者進入地圖選點流程，不猜目的地。
- 若 OTP 找不到方案、座標不可用或 service 不可用，API 要明確回錯誤，不用 LLM 補答案。

## OTP 與 ebus 分工

| 能力 | 資料來源 |
|------|----------|
| 該搭哪條路線、上下車站、轉乘 | OpenTripPlanner |
| 排程式預估旅程與班次可行性 | OTP + GTFS |
| 本站下一班車目前到站狀態 | Yunlin ebus |

OTP 負責 GTFS / OSM graph 上的路線規劃，ebus 負責固定站牌的即時到站狀態。兩者不要混在 provider 層；需要整合時由 service / facade 組合結果。

## 後端流程

```text
frontend destination picker
    -> POST /api/route-plans
    -> services.route_plans
    -> providers.otp planConnection
    -> route view model
    -> MapCN coordinates: [lng, lat]
```

主要責任：

- `backend/providers/otp.py`：包裝 OTP GTFS GraphQL `planConnection`、timeout、錯誤處理、itinerary parser。
- `backend/services/route_plans.py`：解析 Kiosk 起點、套用雲林邊界、轉成產品層 route plan 與前端 view model。
- `backend/api/route_plans.py`：驗證 request、映射 HTTP status、回傳前端需要的 shape。
- `backend/otp/README.md`：記錄 GTFS / OSM / OTP graph build 與 service 啟動細節。

## 前端流程

- 路線規劃是全頁 secondary flow，不是首頁主體。
- 使用者在 MapCN 地圖點選或拖曳 destination pin。
- 確認後送出座標；結果頁顯示候選路線、legs 摘要，並用 `[lng, lat]` coordinates 畫線。
- 找不到方案時保留選點狀態，讓使用者重選。

## 已知資料風險

- TDX static GTFS route naming 可能和 ebus Kiosk route code 不完全一致，例如目前資料有 `7000D`，但 Kiosk 可能顯示 `7000B`。
- GTFS route naming 與 ebus route naming 可能需要 mapping。
- Kiosk 站名必須穩定映到 OTP stop 或座標。
- 地圖選點若落在路網不可達點、河道另一側或離站牌過遠，前端與 API 需要清楚引導重選。
- OSM 步行路網可能讓站牌 snap 或轉乘步行結果失真。
- 低班次路線在「現在出發」查詢時常可能無方案，因此保留指定出發時間功能。

## 參考

- [OTP container image](https://docs.opentripplanner.org/en/latest/Container-Image/)
- [OTP APIs](https://docs.opentripplanner.org/en/latest/apis/Apis/)
- [GTFS GraphQL API](https://docs.opentripplanner.org/en/latest/apis/GTFS-GraphQL-API/)
- [GraphQL routing tutorial](https://docs.opentripplanner.org/en/latest/apis/GraphQL-Tutorial/)
