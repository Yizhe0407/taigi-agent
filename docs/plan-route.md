# `plan_route_to_coordinate` 規劃

## 目標

路線規劃補上雲林公車助理的「從這裡要怎麼去」能力，不只回答本站哪班車會到。
產品主流程由前端地圖選點給目的地座標，聊天 Agent 不用文字猜目的地。

第一版以固定站牌 Kiosk 為出發點：

- 起點固定為 `KIOSK_STOP`
- 目的地由前端地圖選點提供座標
- 先支援步行與公車路線規劃
- 回答可搭路線、方向、上下車站、轉乘與預估總時間

## 非目標

第一版暫不處理：

- 任意 A 到 B 路線規劃
- 跨縣市完整旅運規劃
- 聊天文字目的地解析、POI 搜尋與一般地址 geocoding
- 票價、無障礙、騎車與開車路線
- 把即時車輛位置直接合進 OTP graph

## 為什麼用 OTP

ebus 目前適合查固定站牌的即時到站狀態，但它不是完整的路線規劃引擎。
路線規劃需要把站牌、班次、轉乘與步行接駁一起算出來，因此交給
OpenTripPlanner (OTP)：

- GTFS 提供路線、站牌與排程資料
- OpenStreetMap (OSM) 提供道路與步行路網
- OTP 將資料建成 graph，再提供路線規劃 API

本專案只包裝 OTP 的查詢結果，不自行重寫轉乘演算法。

## 產品切面

第一版使用流程：

```text
使用者：「我要規劃路線」
Agent -> 前端進入目的地地圖選點流程
前端 -> 使用者確認 destination latitude / longitude
backend -> plan_route_to_coordinate(latitude, longitude)
backend -> OTP planConnection
前端 -> 用 MapCN 顯示候選路徑與公車行程摘要
```

預期回答應能落到這類資訊：

```text
建議搭 201 往斗六火車站，在斗六火車站下車。
預估約 18 分鐘，這段不用轉乘。
```

若座標格式錯誤、OTP 無路線或 OTP service 不可用，後端必須明確回傳失敗
原因；Agent 不可憑聊天文字編出路線。

## 架構邊界

### 1. OTP service

OTP graph build 與 serve 獨立於 agent runtime。預計放在：

```text
backend/
  otp/
    docker-compose.yml
    data/
      yunlin-gtfs.zip
      yunlin-stop-index.json
      yunlin.osm.pbf
```

build 階段把 GTFS 與 OSM 轉成 graph；serve 階段載入 graph，讓 Python tool
呼叫本機或部署後的 OTP endpoint。

### 2. OTP provider

新增 `backend/tools/otp.py`：

- 包裝 OTP GTFS GraphQL `planConnection` request
- 設定 timeout 與錯誤處理
- 解析 OTP itinerary legs 成 provider dataclasses
- 只保留本專案回答需要的欄位

候選 env：

```bash
OTP_BASE_URL=http://localhost:8081
```

### 3. Kiosk facade

新增 `backend/tools/kiosk_route_planner.py`：

- 從 generated stop index 把 `KIOSK_STOP` 解析成 OTP 可用起點
- 接收前端地圖選點的目的地座標
- 呼叫 `backend/tools/otp.py`
- 組出產品層 `RoutePlan`，並轉成前端 route view model
- 另外保留短摘要 formatter 供 CLI 檢查與 route option 文案使用

這層負責 Kiosk 產品範圍與前端 adapter；OTP provider 不應知道 Kiosk、
MapCN 或 API request 規則。

generated stop index 由 `backend/scripts/update_yunlin_gtfs.py` 產生。腳本用 TDX
county/intercity stop metadata 的 `LocationCityCode=YUN` 找 canonical stop
UID，再與 GTFS graph feed 交集，讓 Kiosk 起點與 GTFS 篩選資料邊界可控。

### 4. Agent 與前端邊界

路線規劃不放進聊天 Agent 的 tool schema。Agent 收到路線規劃意圖時只要求
前端切到地圖選點流程；座標確認後，前端呼叫 `POST /api/route-plans`：

```json
{
  "destination": { "lat": 23.717831598831527, "lng": 120.53840824484192 },
  "departureTime": "2026-05-22T08:00:00+08:00"
}
```

HTTP handler 驗證 request 後呼叫：

```python
def plan_route_to_coordinate(latitude: float, longitude: float) -> RoutePlan:
    ...
```

座標欄位錯誤交給 request schema 回 `422`；可用座標但無公車方案回 `404`；
OTP 或 stop index 不可用回 `503`。

### 5. 前端 route view model

MapCN 的 route component 畫線時吃座標點列，不吃 OTP 原始 itinerary。HTTP
handler 接到 coordinate planner 結果後，應回傳前端 route view model：

```json
{
  "origin": { "name": "雲林科技大學", "lat": 23.695, "lng": 120.534 },
  "destination": { "name": "地圖選點", "lat": 23.7178, "lng": 120.5384 },
  "routes": [
    {
      "id": "option-1",
      "coordinates": [[120.534, 23.695], [120.5384, 23.7178]],
      "duration": 1680,
      "distance": 5100,
      "transferCount": 0,
      "legs": []
    }
  ]
}
```

欄位方向：

- `coordinates` 給 MapCN `MapRoute`，順序固定為 `[lng, lat]`
- `duration` 用秒，`distance` 用公尺，讓 route option UI 可直接顯示
- `legs` 留本專案的 transit 結構，保留步行段、公車路線、上下車站與排程時間
- OTP provider 仍回 domain dataclasses；前端格式由 Kiosk facade adapter 轉換，
  HTTP handler 只驗證 request 與 status code，避免 UI library shape 汙染 OTP 查詢層

### 6. Vue 前端

前端放在 `frontend/`，採 Vue 3、TypeScript、Tailwind CSS 與 shadcn-vue。
地圖元件使用 MapCN Vue 的 MapLibre copy-paste component 子集合；`Map`、
marker 與 `MapRoute` 留在 `src/components/ui/map/`，產品互動放在
`src/features/route-planner/`，讓地圖 library 層與路線規劃狀態分開。

目前前端 route planning slice 已完成：

- 全畫面 Kiosk route planner 畫面
- 雲林科技大學固定起點 marker
- 地圖點選 destination pin
- destination pin 拖曳微調
- destination 確認後呼叫 `POST /api/route-plans`
- 候選 route option、loading、API error 與 legs 摘要
- 將選取方案的 `routes[].coordinates` 交給 `MapRoute` 並 fit viewport

## OTP 查詢策略

本專案以 GTFS 為主要 transit data，先用 OTP 2 的 GTFS GraphQL API。第一版
路線查詢使用 `planConnection`，設定：

- `origin`：Kiosk 起點座標或 OTP stop location
- `destination`：前端地圖選點座標
- `dateTime`：預設目前時間
- `modes`：`WALK` + `BUS`

工具只解析必要 itinerary 欄位：

- `WALK`：步行接駁摘要
- `BUS`：路線代號、路線名稱、方向資訊、上下車站、排程時間
- itinerary：總時間與轉乘次數
- geometry：各 leg 的 encoded polyline，adapter 轉成 MapCN 可畫的
  `[lng, lat]` 點列

## 工具回傳格式

OTP 回應可能含 geometry 與大量 metadata，不直接整包丟到前端。
`plan_route_to_coordinate` 先回 structured `RoutePlan`，由
`route_plan_to_view_model` 產生前端資料；CLI 或短摘要再用 `format_route_plan`
格式化，例如：

```text
規劃結果 1
- 起點：雲林科技大學
- 目的地：地圖選點
- 預估總時間：約 18 分鐘
- 轉乘：0 次
- 行程：
  1. 從雲林科技大學站搭 201，往斗六火車站
  2. 在斗六火車站下車
```

需要步行時才列出步行段；候選 itinerary 過多時先回傳前 1 到 3 筆。
MapCN 用 `coordinates` 畫線，文字摘要只負責 route option 的說明。

## 目的地選取

路線規劃最後需要座標。第一版不要讓自由文字 geocoding 成為產品主路徑。

前端流程：

1. 使用者說要規劃路線
2. 前端顯示地圖 destination picker
3. 使用者點選並確認目的地 pin
4. 前端呼叫 `POST /api/route-plans` 傳 `destination.lat` / `destination.lng`

後端驗證座標基本格式，再交給 OTP 判斷是否可規劃。前端應限制可選範圍、
顯示已選 pin 並要求確認；若 OTP 找不到公車方案，結果頁要保留選點狀態讓使用者
重選。

generated stop index 現在仍有價值：Kiosk 用它把 `KIOSK_STOP` 解析成起點，
GTFS 更新流程用 TDX 雲林 stop metadata 收斂要建進 OTP graph 的路線。

## OTP 與 ebus 分工

| 能力 | 資料來源 |
|------|----------|
| 該搭哪條路線、上下車站、轉乘 | OTP |
| 排程式預估旅程與班次可行性 | OTP + GTFS |
| 本站下一班車目前到站狀態 | yunlin ebus |

第一版 coordinate planner 先回 OTP 規劃。下一步再把 OTP 建議路線接回現有
ebus Kiosk tool，補一句「這條建議路線目前在本站的到站狀態」。

## 實作順序

1. ✅ 取得雲林可用 GTFS 與涵蓋範圍合適的 OSM `.pbf`
2. ✅ 建立 OTP Docker graph build 與 serve 流程
3. ✅ 用 GraphiQL / 實際 coordinate 查詢驗證雲林 route candidates
4. ✅ 實作 `backend/tools/otp.py` GraphQL client、itinerary parser 與 leg geometry
5. ✅ 實作 Kiosk 出發到目的地座標的 `plan_route_to_coordinate`
6. ✅ Agent prompt 明確改成前端地圖選點流程
7. ✅ 加 provider、facade、API 與 tool schema boundary 測試
8. ✅ 把 OTP geometry 轉成 MapCN route view model
9. ✅ 提供 `POST /api/route-plans` 給前端 route planning flow
10. ✅ 實作前端 destination picker 與 route result view
11. 視結果決定是否合併 ebus 即時到站狀態

GTFS 第一步由 `backend/scripts/update_yunlin_gtfs.py` 下載 TDX 靜態 GTFS
bundle 與 TDX stop metadata，過濾後輸出 `backend/otp/data/yunlin-gtfs.zip`
與 `backend/otp/data/yunlin-stop-index.json`。

## 驗收條件

第一版完成時應滿足：

- 本機 OTP graph 可重建且 service 可啟動
- 至少一條直達與一條轉乘案例能由 coordinate planner 回答
- `plan_route_to_coordinate` 不把 OTP 原始大 payload 直接送進 route view model
- route result view 可用 `[lng, lat]` coordinates 在 MapCN 畫出候選路徑
- 座標格式錯誤、找不到路線、OTP timeout 都有明確錯誤輸出
- LLM 在 route planning 問題上不猜目的地，改引導前端地圖選點

## 風險與待確認

- GTFS 的 service calendar、站名與路線代號是否足夠覆蓋雲林產品範圍
- TDX static GTFS 目前能納入公路客運 `7120`、`7126`，但只找到 `7000D`
  而非 Kiosk 會顯示的 `7000B`，需要確認路線變體對規劃回答的影響
- GTFS route naming 與 ebus route naming 是否需要 mapping
- Kiosk 站名是否能穩定映到 OTP stop 或座標
- 地圖選點若落在路網不可達點、河道另一側或離站牌過遠，前端與 OTP 結果頁
  要如何引導重選
- TDX `LocationCityCode=YUN` stop metadata 是否涵蓋所有實際在雲林停靠的
  外縣市業者 stop；缺漏會讓 stop index 與 graph feed 排除該 destination
- OSM 步行路網是否會讓站牌 snap 或轉乘步行結果失真
- THB 長程 bus route 會保留 selected trip 的完整 stop sequence；若只裁 OSM
  bbox stop_times，OTP 可能無法內插 TDX 中間站時間
- 雲林低班次路線在「現在出發」查詢時是否常回無路線，是否需要允許指定出發時間

## 參考

- [OTP container image](https://docs.opentripplanner.org/en/latest/Container-Image/)
- [OTP APIs](https://docs.opentripplanner.org/en/latest/apis/Apis/)
- [GTFS GraphQL API](https://docs.opentripplanner.org/en/latest/apis/GTFS-GraphQL-API/)
- [GraphQL routing tutorial](https://docs.opentripplanner.org/en/latest/apis/GraphQL-Tutorial/)
