# Production deployment

Ubuntu/systemd 主機，照順序做就能部署起來。用正式服務帳號登入，不要用 root。

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
./deploy/update.sh      # 拉 main 最新 commit 部署，失敗自動回退
./deploy/rollback.sh    # 回到上一版
```

## 5.（選用）開通觀測 Dashboard

SigNoz 已經跑起來，bind 在 `127.0.0.1:8085`。要用瀏覽器直接連 `https://signoz.yizhe.dev`
看，走既有的 Cloudflare Tunnel `ai2-school-server`（跟 `llm.`/`asr.`/`tts.`/`ai2.` 同一個，
設定方式見 `docs/cloudflare-model-services.md`）：

1. Cloudflare Dashboard → Zero Trust → 網路 → 連接器 → Cloudflare Tunnels →
   `ai2-school-server` → Public Hostnames → Add a public hostname。
2. Subdomain 填 `signoz`，Domain 選 `yizhe.dev`，Path 留空，Service type 選 `HTTP`，
   URL 填 `localhost:8085`。
3. `Additional application settings → Access` 開啟 `Protect with Access`，建立一個
   Access application 掛在 `signoz.yizhe.dev`，policy 用互動登入（誰能登入自行設定，
   不要沿用 LLM/ASR/TTS 那種 Service Token policy——那是機器對機器用的，人要用瀏覽器
   登入的話需要一般登入方式）。
4. 儲存，確認 `dig +short signoz.yizhe.dev` 有解析，瀏覽器開
   `https://signoz.yizhe.dev` 應該先看到 Access 登入頁，登入後才進 SigNoz。

第一次打開要先建立 SigNoz 自己的 org/帳號（跟 Cloudflare Access 登入是兩層，各自獨立）。

再到 `/etc/taigi-agent/taigi-agent.env` 加：

```dotenv
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
OTEL_SERVICE_NAME=taigi-bus-agent
```

`sudo systemctl restart taigi-agent.service`。細節、已知問題見
`backend/telemetry/README.md`、`docs/observability.md`。

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
/etc/taigi-agent/taigi-agent.env
/var/lib/taigi-agent/            # runtime state（sessions.db、kiosk_config.json）
/etc/systemd/system/taigi-agent.service
/etc/nginx/sites-available/taigi-agent
```
