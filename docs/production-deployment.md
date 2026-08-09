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

Ubuntu 系統套件：

```bash
sudo apt update
sudo apt install -y git curl rsync nginx
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
2. `pnpm install --frozen-lockfile`，建立 frontend production build。
3. 對 backend 執行 `uv sync --locked --no-dev`。
4. 建立新的版本目錄。
5. 將 `.env` 與 `.agent_state/` 改為共享路徑，避免更新覆蓋 secrets/runtime state。
6. 安裝並驗證 systemd、Nginx 設定。
7. 啟動 `taigi-agent.service`，reload Nginx。
8. 驗證 backend health、Nginx proxy 與 frontend SPA。

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

## 9. WebRTC 限制

`/api/voice/offer` 的 HTTP signaling 可以經由 HTTPS/Cloudflare 到達 backend；Cloudflare Tunnel 不會自動替 WebRTC media 提供 STUN/TURN。若 kiosk 與 backend 不在同一個可直連網路，仍需設定可達的 STUN/TURN/ICE 路徑，否則 voice 連線可能只成功 signaling、無法傳音訊。
