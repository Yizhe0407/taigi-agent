# Production deployment

本部署方案適用於一台 Ubuntu/systemd 主機：

```text
Cloudflare Tunnel（之後設定）
    -> Nginx 127.0.0.1:3000
         /       -> frontend/dist
         /api/*  -> backend 127.0.0.1:8080

systemd taigi-agent.service
    -> uvicorn api:app --workers 1
```

模型服務可繼續使用 `127.0.0.1:8000`；Taigi backend 使用 `127.0.0.1:8080`，避免與模型服務衝突。

## 1. 主機前置條件

以正式服務帳號登入 repo 所在主機；不要用 root 執行部署腳本。

必要套件與指令：

- Python 3.12+
- `uv`
- Node.js + `pnpm`
- `git`、`rsync`、`curl`、`nginx`、`systemctl`、`sudo`
- Docker + Docker Compose plugin（跑 `backend/telemetry/` 的 SigNoz stack）

Ubuntu 系統套件：

```bash
sudo apt update
sudo apt install -y git curl rsync nginx
# Docker：依官方文件安裝 docker-ce + docker-compose-plugin，並把 APP_USER 加進 docker 群組
# https://docs.docker.com/engine/install/ubuntu/
sudo systemctl enable --now docker
```

`uv`、Node.js、`pnpm` 依主機既有標準安裝。確認：

```bash
python3 --version
uv --version
node --version
pnpm --version
nginx -v
```

## 2. 設定環境檔

第一次安裝前，repo 內必須有 `backend/.env`。部署腳本會把它複製到：

```text
/etc/taigi-agent/taigi-agent.env
```

正式環境至少需要：

```dotenv
LLM_BASE_URL=http://127.0.0.1:8000/v1
LLM_MODEL=實際模型名稱
ADMIN_TOKEN=至少24字元的高熵隨機值
```

若模型、ASR、TTS 透過 Cloudflare Access 保護，另填：

```dotenv
CF_ACCESS_CLIENT_ID=...
CF_ACCESS_CLIENT_SECRET=...
ASR_BASE_URL=https://...
ASR_MODEL=...
TTS_BASE_URL=https://...
TTS_MODEL=...
TTS_VOICE=...
```

腳本會拒絕缺少必要值、範例值、短於 24 字元的 `ADMIN_TOKEN`，以及只設定一半的 Cloudflare Access 或 ASR 設定，並將正式環境檔設為 mode `600`。

## 3. 第一次安裝

在 repo 根目錄執行：

```bash
cd /path/to/taigi-agent
./deploy/install.sh
```

部署腳本必須由正式服務帳號執行；不可用 root，也不可用另一個帳號代跑。
`APP_USER` 預設為目前帳號。

腳本執行內容：

1. 驗證 Git working tree 乾淨。
2. 啟動/更新可觀測性 stack（SigNoz，`docker compose up -d`，失敗僅警告不中斷安裝）。
3. `pnpm install --frozen-lockfile`，建立 frontend production build。
4. 對 backend 執行 `uv sync --locked --no-dev`。
5. 建立新的版本目錄。
6. 將 `.env` 與 `.agent_state/` 改為共享路徑，避免更新覆蓋 secrets/runtime state。
7. 安裝並驗證 systemd、Nginx 設定。
8. 啟動 `taigi-agent.service`，reload Nginx。
9. 等待 backend ready，再驗證 backend health、Nginx proxy、frontend SPA，並提醒（不擋部署）TURN 與 SigNoz 是否可連。

## 4. 版本與檔案位置

```text
/opt/taigi-agent/
├── current -> releases/<active-release>
├── previous -> releases/<previous-release>
└── releases/<timestamp>-<git-sha>/
    ├── backend/
    │   └── .venv/
    └── frontend/dist/

/etc/taigi-agent/taigi-agent.env
/var/lib/taigi-agent/
/etc/systemd/system/taigi-agent.service
/etc/nginx/sites-available/taigi-agent
/etc/nginx/sites-enabled/taigi-agent
```

`current` 與 `previous` 使用原子 symlink 切換。backend 固定單 worker，因為 session SQLite、kiosk state 與 process-local rate limit 尚未支援多 worker。

## 5. 更新

正式流程先在本機完成測試、commit、push；再到正式主機執行：

```bash
cd /path/to/taigi-agent
./deploy/update.sh
```

`update.sh` 只接受目前分支為 `main` 且 working tree 乾淨，執行 `fetch` + `merge --ff-only`，完成新 release 後才切換 `current`。systemd/Nginx 安裝、重啟或 health 驗證失敗時，會自動恢復舊 release、舊版服務設定與原本的 `previous` 指標。

若 remote 或 branch 不同：

```bash
DEPLOY_REMOTE=origin DEPLOY_BRANCH=main ./deploy/update.sh
```

## 6. 回滾

回到上一版：

```bash
./deploy/rollback.sh
```

指定已存在的 release：

```bash
ls -1 /opt/taigi-agent/releases
./deploy/rollback.sh 20260809T120000Z-f41ed16
```

回滾會套用目標 release 內的 systemd/Nginx template、重啟 backend、reload Nginx，再驗證 `/api/health` 與首頁。失敗時會嘗試恢復原本的程式、服務設定與 `previous` 指標。

## 7. 手動檢查與服務管理

```bash
./deploy/verify.sh
sudo systemctl status taigi-agent.service
sudo journalctl -u taigi-agent.service -f
sudo nginx -t
sudo systemctl reload nginx
```

本機 endpoint：

```bash
curl -fsS http://127.0.0.1:8080/api/health
curl -fsS http://127.0.0.1:3000/api/health
curl -I http://127.0.0.1:3000/
```

Nginx 只 listen loopback；Cloudflare Tunnel 應指向：

```text
http://127.0.0.1:3000
```

正式前端不設定 `VITE_API_BASE_URL` 時，瀏覽器會用 same-origin `/api/*`，不需要公開 backend port 或設定 CORS。

## 8. 開機自動恢復

安裝腳本會執行：

```bash
sudo systemctl enable taigi-agent.service
sudo systemctl enable nginx
```

systemd backend 使用 `Restart=on-failure`。Cloudflare Tunnel、LLM、ASR、TTS 是否開機啟動，取決於各自的 systemd service；它們不是由本 repo 的 `taigi-agent.service` 啟動。模型服務必須先能在 `LLM_BASE_URL`、`ASR_BASE_URL`、`TTS_BASE_URL` 提供服務。

## 9. 公網 WebRTC / Cloudflare Realtime TURN

Cloudflare Tunnel 只轉送 `/api/voice/offer` signaling，不轉送 WebRTC media。公網語音必須另外設定 Cloudflare Realtime TURN：

1. 在 Cloudflare Dashboard 的 Realtime TURN 建立 TURN key。
2. 將 persistent key ID 與 API token 只寫入正式主機的 `/etc/taigi-agent/taigi-agent.env`：

   ```dotenv
   CLOUDFLARE_TURN_KEY_ID=...
   CLOUDFLARE_TURN_KEY_API_TOKEN=...
   CLOUDFLARE_TURN_TTL_SECONDS=86400
   ```

3. 不得把 TURN API token 寫進 frontend、Git 或瀏覽器。Backend 會向 Cloudflare 換取短效 credentials，快取到到期前，再由 `/api/voice/ice-servers` 回傳給 browser；同一組 ICE servers 也會套用到 server-side aiortc。
4. 更新環境後重啟並檢查：

   ```bash
   sudo systemctl restart taigi-agent.service
   curl -fsS http://127.0.0.1:3000/api/voice/ice-servers
   sudo journalctl -u taigi-agent.service -n 100 --no-pager
   ```

`/api/voice/ice-servers` 應回傳含 `turn:` 或 `turns:` 的 `iceServers`，但不得在文件、issue 或 log 貼出其中的短效 username/credential。從外網開啟語音後，journal 的 ICE state 應進入 `connected`/`completed`，不應在約 60 秒後 timeout。

## 10. 可觀測性（SigNoz）

`deploy/install.sh`/`deploy/update.sh` 會自動對 `backend/telemetry/` 執行
`docker compose up -d`，啟動 SigNoz（OTLP collector + ClickHouse + Postgres）。
這一步失敗只印警告、不會中斷 taigi-agent 本身的部署（這包 stack 仍在踩坑階段，
細節見 `backend/telemetry/README.md` 的「已知坑」）。

UI 與 OTLP port 都只 bind `127.0.0.1`（8085 / 4317 / 4318），不對外開，也沒有子網域。
要看 dashboard 得先 SSH tunnel：

```bash
ssh -L 8085:127.0.0.1:8085 <正式主機>
# 本機開 http://127.0.0.1:8085
```

**第一次啟動必做**：SSH tunnel 進去完成 SigNoz 的註冊精靈（建立 org + admin 帳號），
在完成之前 OTLP 送進來的 span/metric 都會被拒絕（沒有 org 可歸屬）。

要讓 backend 實際把 telemetry 送過去，還要在 `/etc/taigi-agent/taigi-agent.env` 打開：

```dotenv
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
OTEL_SERVICE_NAME=taigi-bus-agent
```

改完 `sudo systemctl restart taigi-agent.service`。內容層級的觀測（user input、
LLM 回應、ASR/TTS 文字）預設會進 SigNoz，正式環境要調整前先看
`docs/observability.md`「Content-level 觀測」一節；不想收集就設
`TELEMETRY_CAPTURE_CONTENT=false`。
