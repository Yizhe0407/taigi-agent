# 路線規劃

使用者在地圖上選擇目的地，系統畫出從 Kiosk 所在站牌出發的候選路線，可包含轉乘。地圖上也可以顯示公共自行車站。功能範圍見 [產品定位](product-positioning.md)。

聊天與地圖的分工：

| 使用者的問題 | 處理方式 |
|---|---|
| 「我要去虎尾」 | 聊天回答本站直達的路線與到站時間（`get_arrivals_to_destination`） |
| 需要轉乘，或說不出目的地名稱 | 使用地圖路線規劃 |
| 跨縣市，或「怎麼轉乘」 | 小芸回答固定說法：目前只能規劃雲林縣內行程 |

目的地座標由使用者在地圖上點選，不讓語言模型從一句話推測座標，避免猜錯。

## 流程

```
地圖選擇目的地 → POST /api/route-plans → OTP 規劃 → 轉成地圖可繪製的路線 → 顯示在地圖上
```

- 起點固定為 Kiosk 所在站牌，可選擇現在出發或指定時間。
- 找不到方案、座標無效或 OTP 未啟動時，API 回傳明確的錯誤，不由 LLM 補充回答。畫面保留已選的位置，讓使用者重新選擇。
- 地圖上可開關雲林的公共自行車站圖層，顯示各站可借車數。

## 資料來源

| 資料 | 來源 |
|---|---|
| 搭乘路線、上下車站、轉乘 | OTP（OpenTripPlanner，使用 GTFS 時刻表與 OSM 路網） |
| 本站下一班的即時狀態 | 交通部 TDX |
| 公共自行車站與可借車數 | TDX；失敗時改用 MOOVO 官網地圖 |

各來源在 provider 層彼此獨立，需要合併時在 service 層組合。

## 相關檔案

| 檔案 | 職責 |
|---|---|
| `backend/providers/otp.py` | 呼叫 OTP、處理逾時與錯誤 |
| `backend/services/route_plans.py` | 起點、雲林邊界、轉成前端格式 |
| `backend/api/route_plans.py` | 驗證輸入、對應 HTTP 狀態 |
| `backend/providers/tdx_bike.py`、`moovo_website.py` | 公共自行車站與可借車數 |
| `backend/otp/README.md` | 下載 GTFS / OSM、建立 OTP graph |

## 已知資料問題

- 路線名稱不一致：GTFS 有 `7000D`，Kiosk 可能顯示 `7000B`。
- 目的地在河的另一側或離站牌太遠時，OSM 步行路網可能使結果失真。
- 班次少的路線在「現在出發」時常查不到方案，因此提供指定出發時間。

參考：[OTP 文件](https://docs.opentripplanner.org/en/latest/)
