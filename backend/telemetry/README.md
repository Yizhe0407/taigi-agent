# SigNoz：看每一輪對話發生了什麼

```bash
docker compose up -d       # UI 在 http://127.0.0.1:8085
```

然後在 `backend/.env` 打開 `OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318`。在 repo root 跑 `process-compose up` 也會一起帶起來。正式環境由 `deploy/install.sh` / `update.sh` 自動啟動（見 [正式部署](../../docs/deployment/production.md)）。怎麼看資料見 [出事時怎麼看](../../docs/observability.md)。

## 第一次啟動必做

打開 UI 完成**註冊精靈**（建 org 和 admin 帳號）。部署腳本不會替你做。

沒註冊前送資料會失敗：4318 連線直接被 reset，SigNoz log 會印 `cannot create agent without orgId`。這是 SigNoz 正常的首次流程，不是壞掉。

## 服務組成

| Service | 做什麼 |
|---|---|
| `signoz-signoz-0` | SigNoz 本體（UI 與查詢） |
| `ingester` | OTLP collector，收 4317 (gRPC) / 4318 (HTTP) |
| `signoz-telemetrystore-clickhouse-0-0` | 主資料庫（trace / metric / log） |
| `signoz-telemetrykeeper-clickhousekeeper-0` | ClickHouse 協調（取代 zookeeper） |
| `signoz-metastore-postgres-0` | SigNoz 自己的設定（dashboard、帳號、alert） |
| `...-migrator`、`...-user-scripts` | 一次性初始化，跑完自動退出 |

單機部署，本機和正式環境用同一份 compose。

## 這份檔案怎麼來的、哪裡手改過

SigNoz 官方改用 [Foundry](https://github.com/SigNoz/foundry) 產生 compose，不再手寫。這裡的檔案是用 `foundryctl forge` 對預設的 `casting.yaml` 產生後放進來的。

**唯一手改的地方**是 port：

| | 預設 | 這裡 | 為什麼 |
|---|---|---|---|
| UI | 8080 | **8085** | 正式 backend 佔 8080 |
| `ingester` | 4317/4318 | 同，但綁 `127.0.0.1` | 不對外網開放 |

UI 只能經 Cloudflare Tunnel + Access 的 `signoz.yizhe.dev` 從外部連（見 production.md 第 5 節）。

**升版後要記得重新套用這兩處 port**，否則會被蓋回預設值，和正式 backend 衝突：

```bash
curl -L "https://github.com/SigNoz/foundry/releases/latest/download/foundry_darwin_$(uname -m | sed 's/x86_64/amd64/').tar.gz" -o /tmp/foundry.tar.gz
tar -xzf /tmp/foundry.tar.gz -C /tmp
/tmp/foundry_darwin_*/bin/foundryctl forge -f casting.yaml -p /tmp/signoz-pours
diff -r /tmp/signoz-pours/deployment .    # 核對差異後手動覆蓋
```

`forge` 只產檔，不啟動 container。

## 資料送進去卻永遠查不到

**症狀**：沒有 error，但 trace 一直是空的。檢查：

```bash
docker exec signoz-telemetrystore-clickhouse-0-0 clickhouse-client \
  --query "SELECT count() FROM system.replicas WHERE is_readonly = 1"
```

回非 0，且 `docker logs signoz-telemetrystore-clickhouse-0-0` 有 `Table is in readonly mode since table metadata was not found in zookeeper`，就是這個問題。

**原因**：ClickHouse 本地的 replica metadata 和 Keeper 裡登記的對不上。兩邊 volume 都還在，所以重啟 container、整個 stack、甚至重啟 Docker 都沒用。（2026-07-09 實測）

**解法只有清 volume 重來**：

```bash
docker compose down -v && docker compose up -d
```

代價：SigNoz 的帳號（在 postgres volume）也會清掉，要重跑註冊精靈。

觸發條件還沒查出來，懷疑是 container 沒有一起乾淨關閉。全新 volume 的乾淨開機沒遇過。

## 其他

- 資料在 named volume，`docker compose down` 不會清；要重置才用 `down -v`。
- 本機預設 port 和專案其他服務（backend 8000、OTP 8081）不衝突。
