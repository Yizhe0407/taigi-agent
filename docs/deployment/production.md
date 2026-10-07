# 正式部署

```
瀏覽器 ──▶ Cloudflare Tunnel ──▶ Nginx :3000 ──┬─ 前端靜態檔
                                               └─ /api/* ──▶ 後端 :8080（systemd，單 worker）
```

適用 Ubuntu + systemd，依序執行以下步驟。請用正式服務帳號登入，不要用 root。本地開發見 [local-development.md](local-development.md)。

## 1. 裝套件

```bash
sudo apt update
sudo apt install -y git curl rsync nginx
```

再裝 `uv`、Node.js + `pnpm`（官方安裝方式即可）。

Docker（給 SigNoz 觀測用，[官方安裝步驟](https://docs.docker.com/engine/install/ubuntu/)）：

```bash
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"
newgrp docker   # 立即套用新群組，不用整個重新登入（只在目前這個 shell 生效）
```

## 2. 填 `backend/.env`

```dotenv
LLM_BASE_URL=http://127.0.0.1:8000/v1
LLM_MODEL=實際模型名稱
ADMIN_TOKEN=至少24字元的高熵隨機值
```

要開放公網語音（WebRTC）再加（Cloudflare Dashboard → Realtime TURN 建 key）：

```dotenv
CLOUDFLARE_TURN_KEY_ID=...
CLOUDFLARE_TURN_KEY_API_TOKEN=...
```

模型/ASR/TTS 若走 Cloudflare Access 保護、或要換 ASR/TTS upstream，完整選填清單看
`backend/.env.example` 抄一份出來改。空著不用管，腳本會擋掉漏填或範例值。

`backend/.env` 是唯一真相源：每次 `install.sh` / `update.sh` 都會把它覆蓋到
`/etc/taigi-agent/taigi-agent.env`（systemd 實際讀的那份）並重啟服務。之後要改設定就改
`backend/.env` 再跑 `./deploy/update.sh`，**不要直接改 `/etc` 那份**，下次部署會被蓋掉。

## 3. 安裝

```bash
cd /path/to/taigi-agent
./deploy/install.sh
```

會自動：建 frontend production build、裝 backend 依賴、裝 systemd + Nginx、啟動服務、
順便把 SigNoz（觀測用）用 docker compose 帶起來、最後自我驗證。跑完看到
`部署驗證通過` 就是成功了。

確認：

```bash
curl -fsS http://127.0.0.1:3000/api/health   # {"status":"ok"}
```

Cloudflare Tunnel 指到 `http://127.0.0.1:3000` 即可，不用開 backend port、不用設 CORS。

## 4. 之後更新 / 回滾

```bash
./deploy/update.sh      # 拉 main 最新 commit + 同步 backend/.env 部署，失敗自動回退
./deploy/rollback.sh    # 回到上一版（只回退程式，env 維持目前那份）
```

## 5.（選用）開觀測 Dashboard

SigNoz 已由安裝腳本帶起來，綁在 `127.0.0.1:8085`。要從瀏覽器看，在既有的 Tunnel `ai2-school-server` 加一個 public hostname：

| 欄位 | 填 |
|---|---|
| Subdomain / Domain | `signoz` / `yizhe.dev` |
| Service | `HTTP`，`localhost:8085` |
| Access | 開 `Protect with Access`，policy 用**互動登入**（不要沿用 LLM/ASR/TTS 的 Service Token policy，那是機器用的） |

存檔後開 `https://signoz.yizhe.dev`，應該先看到 Access 登入頁。第一次進 SigNoz 還要另外建它自己的帳號。

再到 `backend/.env` 加 `OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318`，跑 `./deploy/update.sh`。查詢方式見 [觀測](../observability.md)。

## 常用檢查指令

```bash
./deploy/verify.sh
sudo systemctl status taigi-agent.service
sudo journalctl -u taigi-agent.service -f
sudo nginx -t && sudo systemctl reload nginx
```

## 檔案位置備忘

```text
/opt/taigi-agent/current -> releases/<目前版本>
/etc/taigi-agent/taigi-agent.env  # 由 backend/.env 同步產生，勿手改
/var/lib/taigi-agent/            # runtime state（sessions.db、kiosk_config.json）
/etc/systemd/system/taigi-agent.service
/etc/nginx/sites-available/taigi-agent
```
