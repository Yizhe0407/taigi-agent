# TaiwanBus eBUS API

> 非官方整理：依台灣公路客運即時動態資訊網前端實際呼叫方式整理。
> 測試日期：2026-09-19（Asia/Taipei）。

## 基本設定

```text
Base URL: https://www.taiwanbus.tw/eBUSPage
```

- 下列 API 都使用 `GET`，除非另有註明。
- 回應的 `Content-Type` 常是 `text/html`，但內容實際上是 JSON；請直接嘗試 JSON 解析。
- `lan`：`C`=中文、`E`=英文。
- 陣列參數要用 `[]`，例如 `ClassID[]=53921`。
- 複合 `key` 需先 URL encode。

---

## 1. 路線資料 API：`getRData.ashx`

```text
GET /Query/ws/getRData.ashx
```

### 共用參數

| 參數 | 必填 | 說明 |
|---|---:|---|
| `type` | 是 | 資料種類，見下表 |
| `key` | 視 type | 路線代碼或複合查詢字串 |

### `type` 對照

| `type` | `key` 格式 | 用途 |
|---:|---|---|
| `3` | `{routeKey};{YYYYMMDD}` | 指定日期的完整時刻表 |
| `4` | `{routeKey}` | 路線即時到站、車輛位置 |
| `5` | `{routeKey}` | 路線地圖線段與站牌座標 |
| `6` | `{routeKey}` | 班距／營運時段 |
| `7` | `{routeKey}` | 是否支援預約 |
| `21` | `{routeKey}` | 業者名稱與站牌清單 |
| `22` | 見下方 | 票價查詢 |

### 範例

#### 1.1 即時路線資料 `type=4`

```text
GET /Query/ws/getRData.ashx?type=4&key=020101
```

回應重點：

```json
{
  "time": "22:40:43",
  "data": [],
  "cars": [],
  "stop": [],
  "disColor": "3分,2分,1分,即將進站,進站中,動態發車"
}
```

- `data`：每個站的即時狀態
- `cars`：目前車輛位置
- `stop`：站牌地址
- `time`：資料更新時間

#### 1.2 時刻表 `type=3`

```text
GET /Query/ws/getRData.ashx?type=3&key=020101%3B20260919
```

`key` 內容：

```text
020101;20260919
```

#### 1.3 地圖資料 `type=5`

```text
GET /Query/ws/getRData.ashx?type=5&key=020101
```

回應包含：

- `data.line[]`：編碼過的路線線段
- `data.stop[]`：站名、經緯度、地址、站牌 ID

#### 1.4 路線站牌清單 `type=21`

```text
GET /Query/ws/getRData.ashx?type=21&key=020101
```

回應包含：

- `type`：票價查詢類型
- `customer_name`：客運業者
- `stoplist[]`：`idx`、`stop_id`、`stop_name`

#### 1.5 票價查詢 `type=22`

`key` 格式：

```text
{routeKey};{YYYYMMDD};{startHHMM};{endHHMM};{fromStopId};{toStopId};{ticketType}
```

範例：

```text
GET /Query/ws/getRData.ashx?type=22&key=020101%3B20260919%3B0700%3B0800%3B0%3B13%3B2
```

欄位來源：

| 位置 | 欄位 | 說明 |
|---:|---|---|
| 1 | `routeKey` | 例如 `020101` |
| 2 | `YYYYMMDD` | 搭乘日期 |
| 3 | `startHHMM` | 查詢起始時間，例如 `0700` |
| 4 | `endHHMM` | 起始時間加 1 小時，例如 `0800` |
| 5 | `fromStopId` | `type=21` 的 `stop_id` |
| 6 | `toStopId` | `type=21` 的 `stop_id` |
| 7 | `ticketType` | `type=21` 回傳的 `type` |

---

## 2. 查詢資料 API：`getData.ashx`

```text
GET /Query/ws/getData.ashx
```

### 共用參數

| 參數 | 必填 | 說明 |
|---|---:|---|
| `type` | 是 | 查詢種類 |
| `key` | 視 type | 查詢關鍵值 |
| `lan` | 視 type | `C` 或 `E` |
| `lang` | 視 type | 部分頁面使用，`C` 或 `E` |
| `ClassID[]` | 僅 type 71 | 可傳一個或多個 ClassID |
| `lat` | 僅 type 7 | 緯度 |
| `lng` | 僅 type 7 | 經度 |
| `range` | 僅 type 7 | 搜尋範圍數值 |

### `type` 對照

| `type` | 參數 | 用途 |
|---:|---|---|
| `1` | `key`, `lan` | 路線／站牌關鍵字搜尋 |
| `2` | `lan` | 客運業者清單 |
| `4` | `lan` 或 `lang` | 縣市與鄉鎮清單 |
| `5` | 無 | 交通場站清單 |
| `7` | `lat`, `lng`, `range` | 附近站牌搜尋 |
| `11` | 無 | 舊版全部路線資料；目前實測空回應 |
| `21` | `key`, `lan` | 依客運業者查路線 |
| `31` | `key`, `lan` | 依高鐵站查路線 |
| `33` | `key`, `lan` | 依機場查路線 |
| `34` | `key`, `lan` | 依公車場站查路線 |
| `41` | `key`, `lan` | 依起訖縣市／鄉鎮查路線 |
| `51` | `key`, `lan` | 依起訖交通場站查路線 |
| `71` | `ClassID[]` | 依站牌 ClassID 查路線詳細資料 |
| `321` | `lan` | 縣市與台鐵站清單 |
| `322` | `key`, `lan` | 依台鐵站查路線 |

### 範例

#### 2.1 路線或站牌搜尋 `type=1`

```text
GET /Query/ws/getData.ashx?type=1&key=201&lan=C
GET /Query/ws/getData.ashx?type=1&key=%E6%96%97%E5%85%AD&lan=C
```

`key` 可以是路線號碼或站牌關鍵字。

#### 2.2 客運業者清單 `type=2`

```text
GET /Query/ws/getData.ashx?type=2&lan=C
```

#### 2.3 縣市／鄉鎮清單 `type=4`

```text
GET /Query/ws/getData.ashx?type=4&lan=C
GET /Query/ws/getData.ashx?type=4&lang=E
```

回應格式：

```json
[
  {
    "id": "10017",
    "name": "基隆市",
    "town": [
      {"id": "1001701", "name": "中正區"}
    ]
  }
]
```

#### 2.4 交通場站清單 `type=5`

```text
GET /Query/ws/getData.ashx?type=5
```

#### 2.5 附近站牌 `type=7`

```text
GET /Query/ws/getData.ashx?type=7&lat=23.737&lng=120.417&range=1000
```

回應重點：

- `Name`：站牌名稱
- `Distance`：距離
- `ClassID[]`：可用來找路線
- `StopID[]`：站牌 ID
- `lat`、`lon`：站牌座標

#### 2.6 依客運業者查路線 `type=21`

```text
GET /Query/ws/getData.ashx?type=21&key=%E7%B5%B1%E8%81%AF%E5%AE%A2%E9%81%8B&lan=C
```

`key` 是客運業者名稱，例如：

```text
統聯客運
```

#### 2.7 依交通場站查路線 `type=31/33/34/322`

```text
GET /Query/ws/getData.ashx?type=31&key={stationId}&lan=C
GET /Query/ws/getData.ashx?type=33&key={stationId}&lan=C
GET /Query/ws/getData.ashx?type=34&key={stationId}&lan=C
GET /Query/ws/getData.ashx?type=322&key={stationId}&lan=C
```

| type | 場站來源 |
|---:|---|
| `31` | 高鐵站 ID |
| `33` | 機場 ID |
| `34` | 公車場站 ID |
| `322` | `type=321` 回傳的台鐵站 ID |

#### 2.8 依起訖地區查路線 `type=41`

`key` 格式：

```text
{fromCityId}_,{fromTownId}_,{toCityId}_,{toTownId}
```

範例：

```text
GET /Query/ws/getData.ashx?type=41&key=10017_%2C1001701_%2C65000_%2C6500001&lan=C
```

#### 2.9 依起訖交通場站查路線 `type=51`

`key` 格式：

```text
{fromStationId}_,{toStationId}
```

範例：

```text
GET /Query/ws/getData.ashx?type=51&key=79_%2C80&lan=C
```

#### 2.10 依 ClassID 查路線 `type=71`

必須使用陣列參數：

```text
GET /Query/ws/getData.ashx?type=71&ClassID%5B%5D=53921
GET /Query/ws/getData.ashx?type=71&ClassID%5B%5D=53921&ClassID%5B%5D=53922
```

不要只傳：

```text
ClassID=53921
```

這種格式目前會回空陣列。

#### 2.11 縣市／台鐵站清單 `type=321`

```text
GET /Query/ws/getData.ashx?type=321&lan=C
```

---

## 3. 公告 API：`getNews.ashx`

```text
GET /Query/ws/getNews.ashx
```

| `type` | 其他參數 | 用途 |
|---:|---|---|
| `1` | `lan=C/E` | 最新公告，回傳公告陣列 |
| `4` | 無 | 回傳公告彈出視窗 URL |

範例：

```text
GET /Query/ws/getNews.ashx?type=1&lan=C
GET /Query/ws/getNews.ashx?type=4
```

---

## 4. 語系與文字大小 API

### 4.1 切換語系

```text
POST /Query/ws/lang.ashx
```

參數：無。

### 4.2 取得語系 Cookie

```text
POST /Query/ws/getLangCookie.ashx
```

參數：無。

### 4.3 設定文字大小

```text
POST /Query/ws/textSize.aspx
```

| 參數 | 必填 | 可用值 |
|---|---:|---|
| `size` | 是 | `sm` 小、`md` 預設、`lg` 大 |

範例：

```text
POST /Query/ws/textSize.aspx
Content-Type: application/x-www-form-urlencoded

size=md
```

---

## 5. 網頁型端點

這些端點可以連線，但回傳 HTML，不是 JSON API。

### 5.1 路線查詢頁

```text
GET /Query/QueryResult.aspx
```

| 參數 | 必填 | 說明 |
|---|---:|---|
| `rno` | 是 | 路線頁代碼，例如 `02010` |
| `rn` | 是 | 隨機數或時間戳，例如 `1785285092040` |
| `lan` | 否 | `C` 或 `E` |

### 5.2 路線地圖頁

```text
GET /Query/RMap.aspx?rno={routeKey}
```

| 參數 | 必填 | 說明 |
|---|---:|---|
| `rno` | 是 | 路線 key，例如 `020101` |

### 5.3 每週時刻表頁

```text
GET /json/TimeTableAPIByWeek.aspx
```

| 參數 | 必填 | 說明 |
|---|---:|---|
| `RouteId` | 是 | 4 碼路線代碼，例如 `0201` |
| `SearchDate` | 是 | 日期，格式 `YYYY/MM/DD` |
| `Reserved` | 否 | `1` 表示預約時刻表 |

範例：

```text
GET /json/TimeTableAPIByWeek.aspx?RouteId=0201&SearchDate=2026%2F09%2F19
```

### 5.4 票價頁

```text
GET /json/TMSQuery.aspx
```

| 參數 | 必填 | 說明 |
|---|---:|---|
| `routedata` | 是 | 路線 key，例如 `020101` |
| `app` | 否 | 網站使用 `true` |

範例：

```text
GET /json/TMSQuery.aspx?routedata=020101&app=true
```

---

## 6. 建議的即時公車查詢流程

```text
1. getData type=1
   找到路線或站牌

2. getData type=71
   用 ClassID 對應到實際路線

3. getRData type=21
   取得路線站牌清單

4. getRData type=4
   取得即時到站資料

5. 沒有即時資料時，再呼叫 getRData type=3
   取得固定時刻表
```

目前實測最適合取代舊即時資料來源的是：

```text
getRData.ashx?type=4&key={routeKey}
```
