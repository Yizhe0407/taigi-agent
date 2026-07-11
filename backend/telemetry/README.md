# SigNoz（本機觀測後端）

跑 `docker compose up -d` 即可，UI 在 http://localhost:8080。啟動後把 `.env` 的
`OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318` 打開（見 `backend/.env.example`）。

## 這份檔案哪來的

SigNoz 官方已棄用手寫 docker-compose，改用 [Foundry](https://github.com/SigNoz/foundry)
CLI 動態產生。`docker-compose.yml` 與其餘 config（`ingester/`、`telemetrykeeper/`、
`telemetrystore/`）是用官方 `foundryctl forge` 對 `casting.yaml`（docker compose flavor，
全預設值）產生後原樣 vendor 進來的，沒有手改內容——手改容易跟官方實際架構漂移。

## 升版

```bash
curl -L "https://github.com/SigNoz/foundry/releases/latest/download/foundry_darwin_$(uname -m | sed 's/x86_64/amd64/;s/arm64/arm64/').tar.gz" -o /tmp/foundry.tar.gz
tar -xzf /tmp/foundry.tar.gz -C /tmp
/tmp/foundry_darwin_*/bin/foundryctl forge -f casting.yaml -p /tmp/signoz-pours
diff -r /tmp/signoz-pours/deployment .   # 核對差異後手動覆蓋
```

`forge` 只產檔、不啟動 container；真要本機跑起來另外 `cast`（forge + docker compose up 一次做完）。

## 服務組成（無 zookeeper，改用 ClickHouse Keeper）

| Service | Image | 說明 |
|---|---|---|
| `signoz-signoz-0` | `signoz/signoz:latest` | App（取代舊版 query-service + frontend），port 8080 |
| `ingester` | `signoz/signoz-otel-collector:latest` | OTLP collector，port 4317 (gRPC) / 4318 (HTTP) |
| `signoz-telemetrystore-clickhouse-0-0` | `clickhouse/clickhouse-server` | 主資料庫（trace/metric/log） |
| `signoz-telemetrykeeper-clickhousekeeper-0` | `clickhouse/clickhouse-keeper` | ClickHouse 協調服務 |
| `signoz-metastore-postgres-0` | `postgres:16` | SigNoz 自身 metadata（dashboard、alert 設定等） |
| `signoz-telemetrystore-migrator` | `signoz/signoz-otel-collector` | 一次性 schema migration，跑完自動退出 |
| `signoz-telemetrystore-clickhouse-user-scripts` | `clickhouse/clickhouse-server` | 一次性下載 histogramQuantile UDF，跑完自動退出 |

## 已知坑

- Port 8080/4317/4318 跟本專案其他服務（backend 8000、frontend、`backend/otp` 的 8081）不衝突，已核對過。
- 資料存在 named volume（`signoz-telemetrystore-0-0-data` 等），`docker compose down` 不會清；要重置環境用 `docker compose down -v`。
- 這是單機開發用途，未做叢集/多副本；正式環境另評估。
- **第一次啟動必做**：`docker compose up -d` 起完後，先開 http://localhost:8080
  完成註冊精靈（建立 org + admin 帳號），OTLP 送進來的資料才有 org 可歸屬。
  在完成註冊前送 span/metric 到 4317/4318 會失敗（`ingester` 對 4318 的
  連線直接被 reset，因為 collector 透過 opamp 跟 `signoz-signoz-0` 要完整
  pipeline 設定時被拒絕，signoz app log 會印
  `"cannot create agent without orgId"`）——這是 SigNoz 本身的正常首次啟動
  流程，已用本機環境驗證（container 全綠、`docker compose config` 通過），
  不是這份 vendor 檔案的問題。
- **ClickHouse table 卡 readonly、送什麼都進不去（無 error，但 trace 永遠查不到）**：
  症狀是 `docker exec signoz-telemetrystore-clickhouse-0-0 clickhouse-client --query
  "SELECT count() FROM system.replicas WHERE is_readonly = 1"` 回非 0，
  且 `docker logs signoz-telemetrystore-clickhouse-0-0` 有
  `Table is in readonly mode since table metadata was not found in zookeeper`。
  根因：ClickHouse 本地 metadata（volume 裡持久化的 replica UUID / path）跟
  ClickHouse Keeper 當下實際登記的 znode 對不上——一旦發生，重啟 container、
  重啟整個 stack、甚至重啟 OrbStack 本身都沒用，因為兩邊的 volume 都還在，
  mismatch 會一直被帶著跑。已實測驗證（2026-07-09）。
  **解法只有清 volume 重來**：`docker compose down -v && docker compose up -d`，
  代價是 SigNoz 的 org/帳號（存在 postgres volume）也會一起清掉，
  重開後要重新跑一次 http://localhost:8080 的註冊精靈。
  尚未查出是什麼操作觸發這個 mismatch 第一次發生（懷疑跟不完整關閉
  / container 各自獨立重啟導致 clickhouse-server 與 keeper 不同步有關，
  但沒有再現最小條件）；乾淨開機（volume 全新）目前沒遇過這問題。
