# OTP

地圖路線規劃使用 OpenTripPlanner（OTP）。這個資料夾放它的建置輸入；下載的 GTFS、OSM 和 graph 都不進 git。為什麼這樣設計見 [路線規劃](../../docs/route-planning.md)。

```
TDX GTFS ─┐
          ├─▶ OTP build ─▶ graph ─▶ OTP :8081 ◀── POST /api/route-plans
OSM 路網 ─┘
```

## 1. 準備 GTFS（時刻表）

TDX 提供全台 GTFS。這支腳本下載後只留雲林，並產生雲林站牌索引：

```bash
cd backend
uv run python scripts/update_yunlin_gtfs.py --env-file <.env 路徑>
```

需要 `TDX_CLIENT_ID` / `TDX_CLIENT_SECRET`（先讀環境變數，再讀 `--env-file`）。產出：

```text
otp/data/yunlin-gtfs.zip
otp/data/yunlin-stop-index.json
```

已經下載過全台 GTFS 的話，用 `--input <zip> --output otp/data/yunlin-gtfs.zip` 跳過下載（仍會連 TDX 更新站牌索引）。

判定「雲林的路線」的規則：`YUN_` 開頭業者的路線，或至少有一個站的 TDX `LocationCityCode` 是 `YUN`。所以公總的 `7120`、`7126` 也會被留下。留下的班次會保留完整停靠序列（不裁切），因為 OTP 要靠最後一站的時間內插，裁掉會讓 graph build 失敗。

已知資料問題：TDX 靜態 GTFS 有 `7000D`，Kiosk 上顯示的是 `7000B`，需要人工確認。

## 2. 準備 OSM（路網）

`otp/data/yunlin.osm.pbf` 不在 git 裡。重做：

```bash
curl -L -o /tmp/taiwan-latest.osm.pbf https://download.geofabrik.de/asia/taiwan-latest.osm.pbf   # 約 300 MB
brew install osmium-tool
osmium extract -b 119.88,23.31,120.84,23.97 /tmp/taiwan-latest.osm.pbf -o otp/data/yunlin.osm.pbf
```

範圍是雲林縣邊界（OSM relation 2915930）外加約 0.1° 緩衝，這樣跨縣的 `7120`、`7126` 才不會在路中間被切掉。

## 3. 建 graph 並啟動

```bash
cd backend
docker run --rm -e JAVA_TOOL_OPTIONS="-Xmx4g" \
  -v "$(pwd)/otp/data:/var/opentripplanner" \
  docker.io/opentripplanner/opentripplanner:2.9.0 --build --save

docker compose -f otp/docker-compose.yml up -d
```

GraphQL 在 `http://localhost:8081/otp/gtfs/v1`。前端不直接打 OTP，而是打 `POST /api/route-plans`，由後端回傳地圖可畫的路線。

目前只用 BUS 模式。已驗證可用 `planConnection` 從雲科大站規劃到地圖上任選的雲林座標。
