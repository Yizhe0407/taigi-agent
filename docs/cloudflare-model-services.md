# Cloudflare Tunnel + Access 模型服務設定

## 目標

本文件完成以下串接：已部署的 LLM、ASR、TTS → 同一個 Cloudflare Tunnel → Access Service Token → 本專案後端。

本文件不處理模型訓練或 ASR／TTS runtime 安裝。三個 origin 必須先在模型主機啟動；未監聽指定 port 時先修模型服務，不要繼續設定 Tunnel。

| 服務 | 模型主機 Origin | Public hostname | API |
| --- | --- | --- | --- |
| LLM | `http://localhost:8000` | `https://llm.yizhe.dev` | OpenAI compatible `/v1` |
| ASR | `http://localhost:9000` | `https://asr.yizhe.dev` | `/v1/audio/transcriptions` |
| TTS | `http://localhost:5000` | `https://tts.yizhe.dev` | `/v1/audio/speech` |

保留既有 SSH 路由：

```text
ai2.yizhe.dev/* → ssh://localhost:22
```

三個 HTTP hostname 使用同一個 remotely managed Tunnel `ai2-school-server`。不要建立三個 `cloudflared` daemon。

模型主機的 LLM 使用 port `8000`。專案後端應在另一台開發／Kiosk 主機執行；若必須同機執行，後端改用其他 port。

## 1. 確認 Origin 服務

在模型主機執行：

```bash
set -euo pipefail

sudo ss -ltnp | grep -E ':(22|5000|8000|9000)\b'
systemctl is-active cloudflared
systemctl is-active llama-4b 2>/dev/null || true
systemctl is-active docker 2>/dev/null || true
sudo docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}' 2>/dev/null || true

curl --connect-timeout 5 --max-time 30 -sS \
  http://localhost:8000/v1/models \
  | grep -F 'unsloth/Qwen3.5-4B-GGUF:Q8_0'

origin_tts_headers=$(mktemp)
origin_asr_body=$(mktemp)
trap 'rm -f "$origin_tts_headers" "$origin_asr_body"' EXIT

origin_tts_code=$(curl --connect-timeout 5 --max-time 180 -sS \
  -H 'Content-Type: application/json' \
  -d '{"model":"taigi_text_epoch3419","voice":"taigi_text_epoch3419","input":"逐家好，這是 origin 測試。"}' \
  -D "$origin_tts_headers" \
  -o /tmp/taigi-origin-test.wav \
  -w '%{http_code}' \
  http://localhost:5000/v1/audio/speech)
test "$origin_tts_code" = 200
grep -Eqi '^content-type: audio/(wav|x-wav)' "$origin_tts_headers"
test -s /tmp/taigi-origin-test.wav
file /tmp/taigi-origin-test.wav | grep -qi 'WAVE audio'

origin_asr_code=$(curl --connect-timeout 5 --max-time 180 -sS \
  -F 'file=@/tmp/taigi-origin-test.wav;type=audio/wav' \
  -F 'model=breeze-asr-26' \
  -o "$origin_asr_body" -w '%{http_code}' \
  http://localhost:9000/v1/audio/transcriptions)
test "$origin_asr_code" = 200
grep -Eq '"text"[[:space:]]*:' "$origin_asr_body"
cat "$origin_asr_body"

rm -f "$origin_tts_headers" "$origin_asr_body"
trap - EXIT
```

必須同時確認：

```text
22    SSH
5000  TTS，回傳 PCM WAV
8000  LLM，模型列表含 Qwen3.5-4B Q8_0
9000  ASR，JSON 含 text
```

`llama-4b` 是目前 LLM 的 systemd unit；ASR／TTS 若由 Docker 執行，以 `docker ps` 顯示的實際 container name 管理。任何檢查失敗時先修復 origin。

LLM 4B 啟動參數見 [`docs/llama.md`](llama.md)。

## 2. 建立或確認 Tunnel connector

`cloudflared` 必須和三個 origin 在同一台模型主機、同一個 network namespace 執行，因為 route 使用 `localhost`。若 `cloudflared` 放在 container，改用 host networking 或可從 container 連到的主機位址，不能直接沿用 `localhost`。

Cloudflare Dashboard：

```text
Zero Trust → 網路 → 連接器 → Cloudflare Tunnels
```

使用：

```text
Tunnel：ai2-school-server
模式：remotely managed
```

若 `ai2-school-server` 不存在才建立 Tunnel。若只是增加或重裝 connector，必須加入既有 `ai2-school-server`，並執行 Dashboard 為該 Tunnel 產生的安裝命令。不要把 Tunnel token 寫入 Git。

在模型主機啟用並立即啟動 connector：

```bash
sudo systemctl enable --now cloudflared
sudo systemctl status cloudflared --no-pager
sudo journalctl -u cloudflared -n 50 --no-pager
```

Dashboard 中 `ai2-school-server` 必須顯示 `Healthy`。

## 3. 建立 Service Token

Cloudflare Dashboard：

```text
Zero Trust → Access 控制 → 服務認證 → Service Tokens
```

建立：

```text
名稱：taigi-agent-ai2-models
期限：依維運需求設定
```

建立後立即保存：

```text
Client ID
Client Secret
```

兩者只放在 `backend/.env` 或正式 secret manager，不放入文件、Git、shell script 或 systemd unit。

## 4. 建立 reusable policies

Cloudflare Dashboard：

```text
Zero Trust → Access 控制 → 原則 → 新增原則
```

建立三條 policy：

| Policy name | Action | Include selector | Value |
| --- | --- | --- | --- |
| `taigi-agent-llm-service-token` | `Service Auth`／`服務驗證` | `Service Token` | `taigi-agent-ai2-models` |
| `taigi-agent-asr-service-token` | `Service Auth`／`服務驗證` | `Service Token` | `taigi-agent-ai2-models` |
| `taigi-agent-tts-service-token` | `Service Auth`／`服務驗證` | `Service Token` | `taigi-agent-ai2-models` |

操作順序：

1. Include selector 選 `Service Token`。
2. Value 搜尋 `taigi-agent-ai2-models`。
3. 等候下拉選項載入。
4. 點選下拉選項，確認欄位出現可移除的 token 標籤。
5. Action 選 `Service Auth`，不能選 `Allow`。
6. 填入 policy name。
7. 儲存。

若出現：

```text
include field should not be empty
```

代表只輸入了文字，沒有實際點選 Service Token 下拉選項。

## 5. 建立 Access applications

先建立 Access applications，再新增 Tunnel public hostnames。

Cloudflare Dashboard：

```text
Zero Trust → Access 控制 → 應用程式 → 建立新應用程式
→ Self-hosted and private
```

建立：

| Application name | Public hostname | Existing policy |
| --- | --- | --- |
| `taigi-agent-llm` | `llm.yizhe.dev` | `taigi-agent-llm-service-token` |
| `taigi-agent-asr` | `asr.yizhe.dev` | `taigi-agent-asr-service-token` |
| `taigi-agent-tts` | `tts.yizhe.dev` | `taigi-agent-tts-service-token` |

每個 application：

1. 填 Application name。
2. Add public hostname。
3. 填 subdomain；domain 選 `yizhe.dev`；path 留空。
4. Access policies 選 `Add existing policy`。
5. 選取對應的 reusable policy。
6. 若有 `401 Response for Service Auth policies`，啟用。
7. 儲存 application。

每個 application 必須只有對應的 `Service Auth` policy。不要加入：

```text
Bypass
Everyone
Allow email
```

不要修改既有 `dev` application。

## 6. 新增 Tunnel public hostnames

Cloudflare Dashboard：

```text
Zero Trust → 網路 → 連接器 → Cloudflare Tunnels
→ ai2-school-server
→ Published application routes／Public Hostnames
```

新增：

| Subdomain | Domain | Path | Service type | URL | Access application |
| --- | --- | --- | --- | --- | --- |
| `llm` | `yizhe.dev` | 留空 | `HTTP` | `localhost:8000` | `taigi-agent-llm` |
| `asr` | `yizhe.dev` | 留空 | `HTTP` | `localhost:9000` | `taigi-agent-asr` |
| `tts` | `yizhe.dev` | 留空 | `HTTP` | `localhost:5000` | `taigi-agent-tts` |

每條 HTTP route：

1. 填 hostname、service type 與 URL。
2. 在 `Additional application settings → Access` 啟用 `Protect with Access`。
3. 從**該條 route 對應的 Access application overview** 複製 `Team name` 與 `Application Audience (AUD) Tag`。`Team name` 應是同一個 Zero Trust team；但 LLM、ASR、TTS 的 AUD 必須分別使用自己的 application AUD，不可交叉貼用。
4. 儲存 route。

儲存後逐一核對 application overview 的 hostname 與 route：
`llm.yizhe.dev ↔ taigi-agent-llm`、`asr.yizhe.dev ↔ taigi-agent-asr`、
`tts.yizhe.dev ↔ taigi-agent-tts`。第 11～13 節的三個帶 Service Token 測試都回 `200`
才代表 Team name／AUD mapping 正確；若只有某一條回 `403`，先檢查該 route 使用的 AUD 是否拿錯 application。

完成後確認：

```text
ai2.yizhe.dev/* → ssh://localhost:22
llm.yizhe.dev   → http://localhost:8000
asr.yizhe.dev   → http://localhost:9000
tts.yizhe.dev   → http://localhost:5000
```

不可刪除或修改 SSH route。

## 7. 設定後端

建立環境檔：

```bash
cd backend
test -f .env || cp .env.example .env
chmod 600 .env
```

設定：

```dotenv
LLM_BASE_URL=https://llm.yizhe.dev/v1
LLM_MODEL=unsloth/Qwen3.5-4B-GGUF:Q8_0
LLM_API_KEY=ollama

ADMIN_TOKEN=<RANDOM_32_BYTE_SECRET>

ASR_BASE_URL=https://asr.yizhe.dev
ASR_MODEL=breeze-asr-26
ASR_API_KEY=

TTS_BASE_URL=https://tts.yizhe.dev
TTS_MODEL=taigi_text_epoch3419
TTS_VOICE=taigi_text_epoch3419
TTS_API_KEY=

CF_ACCESS_CLIENT_ID=<SERVICE_TOKEN_CLIENT_ID>
CF_ACCESS_CLIENT_SECRET=<SERVICE_TOKEN_CLIENT_SECRET>
```

本專案會自動把以下 headers 加到 LLM、ASR、voice STT 與 TTS upstream requests：

```http
CF-Access-Client-Id: <SERVICE_TOKEN_CLIENT_ID>
CF-Access-Client-Secret: <SERVICE_TOKEN_CLIENT_SECRET>
```

`CF_ACCESS_CLIENT_ID` 與 `CF_ACCESS_CLIENT_SECRET` 必須同時設定。只設定其中一個時，後端會直接報錯，不會送出未完整驗證的請求。

## 8. 驗證 DNS 與 route

先確認公開 DNS 有解析：

```bash
for host in llm.yizhe.dev asr.yizhe.dev tts.yizhe.dev; do
  printf '=== %s ===\n' "$host"
  dig +short "$host"
done
```

三個 hostname 都必須有結果。`dig` 只能證明 DNS 可解析。

再到 Cloudflare Dashboard 的 DNS records 確認三筆 hostname 都由 Tunnel 建立、狀態為 proxied，並在 `ai2-school-server` 的 Published application routes 中核對 origin。未授權請求收到 `401`／`403` 只證明 DNS 與 Access edge 生效；connector `Healthy` 且帶 Token 的模型測試回 `200`，才證明 route 與 origin 已串通。

## 9. 驗證未授權請求

```bash
checks=(
  'llm.yizhe.dev/v1/models'
  'asr.yizhe.dev/v1/audio/transcriptions'
  'tts.yizhe.dev/v1/audio/speech'
)

for endpoint in "${checks[@]}"; do
  code=$(curl --connect-timeout 15 --max-time 30 \
    -sS -o /dev/null -w '%{http_code}' \
    "https://${endpoint}")
  printf '%-48s HTTP %s\n' "$endpoint" "$code"
  case "$code" in
    401|403) ;;
    *) echo "FAIL: Access 未正確阻擋 ${endpoint}" >&2; exit 1 ;;
  esac
done
```

三個 endpoint 都必須回 `401` 或 `403`：

- `200`：Access 未套用。
- `302`：通常套用了互動式登入 policy；模型 API 應改成 `Service Auth`。
- `404`／`502`：hostname、Tunnel route 或 origin 錯誤。

## 10. 載入測試憑證

在 repo root 執行：

```bash
set -a
source backend/.env
set +a

access_headers=(
  -H "CF-Access-Client-Id: $CF_ACCESS_CLIENT_ID"
  -H "CF-Access-Client-Secret: $CF_ACCESS_CLIENT_SECRET"
)
```

不要使用 `echo`、`set -x` 或 `env` 輸出憑證。

## 11. 測試 LLM

同時驗證模型列表與實際推論：

```bash
set -euo pipefail

llm_models_body=$(mktemp)
llm_chat_body=$(mktemp)
trap 'rm -f "$llm_models_body" "$llm_chat_body"' EXIT

llm_models_code=$(curl --connect-timeout 15 --max-time 90 -sS \
  "${access_headers[@]}" \
  -o "$llm_models_body" -w '%{http_code}' \
  "${LLM_BASE_URL%/}/models")
test "$llm_models_code" = 200
grep -F "$LLM_MODEL" "$llm_models_body"

llm_chat_code=$(curl --connect-timeout 15 --max-time 180 -sS \
  "${access_headers[@]}" \
  -H 'Content-Type: application/json' \
  -d "{\"model\":\"$LLM_MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"只回答 OK\"}],\"max_tokens\":16,\"temperature\":0}" \
  -o "$llm_chat_body" -w '%{http_code}' \
  "${LLM_BASE_URL%/}/chat/completions")
test "$llm_chat_code" = 200
grep -Eq '"choices"[[:space:]]*:' "$llm_chat_body"
grep -Eq '"content"[[:space:]]*:' "$llm_chat_body"
cat "$llm_chat_body"

rm -f "$llm_models_body" "$llm_chat_body"
trap - EXIT
```

兩個請求都必須是 HTTP `200`；模型列表必須包含 `LLM_MODEL`，chat completion 必須含 `choices` 與 `content`。

## 12. 測試 TTS

```bash
set -euo pipefail

tts_headers=$(mktemp)
trap 'rm -f "$tts_headers"' EXIT

tts_code=$(curl --connect-timeout 15 --max-time 180 -sS \
  "${access_headers[@]}" \
  -H 'Content-Type: application/json' \
  -d "{\"model\":\"$TTS_MODEL\",\"voice\":\"$TTS_VOICE\",\"input\":\"逐家好，這是語音測試。\"}" \
  -D "$tts_headers" \
  -o /tmp/taigi-test.wav \
  -w '%{http_code}' \
  "${TTS_BASE_URL%/}/v1/audio/speech")

test "$tts_code" = 200
grep -Eqi '^content-type: audio/(wav|x-wav)' "$tts_headers"
test -s /tmp/taigi-test.wav
file /tmp/taigi-test.wav | grep -qi 'WAVE audio'
wc -c /tmp/taigi-test.wav

rm -f "$tts_headers"
trap - EXIT
```

必須同時符合：HTTP `200`、`Content-Type` 為 WAV、檔案非空、`file` 辨識為 WAVE audio。

## 13. 測試 ASR

使用上一節產生的 WAV：

```bash
set -euo pipefail

asr_body=$(mktemp)
trap 'rm -f "$asr_body"' EXIT

asr_code=$(curl --connect-timeout 15 --max-time 180 -sS \
  "${access_headers[@]}" \
  -F 'file=@/tmp/taigi-test.wav;type=audio/wav' \
  -F "model=$ASR_MODEL" \
  -o "$asr_body" -w '%{http_code}' \
  "${ASR_BASE_URL%/}/v1/audio/transcriptions")

test "$asr_code" = 200
grep -Eq '"text"[[:space:]]*:' "$asr_body"
cat "$asr_body"

rm -f "$asr_body"
trap - EXIT
```

必須是 HTTP `200` 且 JSON 含 `text`。文字不準但 API 正常時，另查 TTS 音質、ASR 模型與音訊格式。

## 14. 驗證 SSH route

先確認：

- `ai2.yizhe.dev/* → ssh://localhost:22` 仍在同一 Tunnel。
- 既有 SSH Access application 仍涵蓋 `ai2.yizhe.dev`，且登入帳號符合其 `Allow` policy。
- 用戶端已安裝 `cloudflared`。

```bash
SSH_USER=ai2
cloudflared_bin="$(command -v cloudflared)" || {
  echo 'cloudflared not found' >&2
  exit 1
}

ssh -o "ProxyCommand=$cloudflared_bin access ssh --hostname %h" \
  "$SSH_USER@ai2.yizhe.dev"
```

登入後執行：

```bash
hostname
systemctl is-active cloudflared llama-4b
sudo ss -ltnp | grep -E ':(22|5000|8000|9000)\b'
```

## 15. 啟動專案

先在 `backend/.env` 設定高熵 `ADMIN_TOKEN`，不能保留 `.env.example` 的公開範例值。

在專案主機執行：

```bash
command -v uv >/dev/null || {
  echo 'uv not found; install the README prerequisite first' >&2
  exit 1
}
uv --version

cd backend
uv sync
uv run uvicorn api:app --host 127.0.0.1 --port 8000
```

若專案後端與 LLM 在同一台主機，port `8000` 已被 LLM 使用；後端改用未占用的 port，例如：

```bash
uv run uvicorn api:app --host 127.0.0.1 --port 8080
```

只有在受防火牆或 reverse proxy 保護且確實需要 LAN 存取時才綁 `0.0.0.0`。修改 `backend/.env` 後必須重新啟動後端 process。

## 16. 專案端到端 smoke test

保持後端執行，另開一個 repo root terminal。若後端使用 `8080`，同步修改 `BACKEND_URL`。

```bash
set -euo pipefail

BACKEND_URL=http://127.0.0.1:8000
session_body=$(mktemp)
chat_body=$(mktemp)
backend_asr_body=$(mktemp)
backend_tts_headers=$(mktemp)
trap 'rm -f "$session_body" "$chat_body" "$backend_asr_body" "$backend_tts_headers"' EXIT

session_code=$(curl --connect-timeout 5 --max-time 30 -sS \
  -X POST -o "$session_body" -w '%{http_code}' \
  "$BACKEND_URL/api/chat/sessions")
test "$session_code" = 200
session_id=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["sessionId"])' "$session_body")

curl --connect-timeout 5 --max-time 180 -N -sS \
  -H 'Content-Type: application/json' \
  -d '{"message":"不要使用工具，請只回答：模型連線成功。"}' \
  "$BACKEND_URL/api/chat/sessions/$session_id/messages/stream" \
  -o "$chat_body"
grep -F '"done": true' "$chat_body"
if grep -Fq '"error":' "$chat_body"; then
  cat "$chat_body" >&2
  exit 1
fi

backend_tts_code=$(curl --connect-timeout 5 --max-time 180 -sS \
  -H 'Content-Type: application/json' \
  -d '{"text":"逐家好，這是專案端到端測試。"}' \
  -D "$backend_tts_headers" \
  -o /tmp/taigi-backend-test.wav \
  -w '%{http_code}' \
  "$BACKEND_URL/api/tts")
test "$backend_tts_code" = 200
grep -Eqi '^content-type: audio/(wav|x-wav)' "$backend_tts_headers"
test -s /tmp/taigi-backend-test.wav
file /tmp/taigi-backend-test.wav | grep -qi 'WAVE audio'

backend_asr_code=$(curl --connect-timeout 5 --max-time 180 -sS \
  -F 'file=@/tmp/taigi-backend-test.wav;type=audio/wav' \
  -o "$backend_asr_body" -w '%{http_code}' \
  "$BACKEND_URL/api/asr")
test "$backend_asr_code" = 200
grep -Eq '"text"[[:space:]]*:' "$backend_asr_body"
cat "$backend_asr_body"

rm -f "$session_body" "$chat_body" "$backend_asr_body" "$backend_tts_headers"
trap - EXIT
```

全部成功才代表本專案已透過 Access Service Token 使用 LLM、TTS、ASR。

## 17. 開機啟動與存取邊界

`cloudflared` 自動啟動只會啟動 Tunnel connector，不會連帶啟動 LLM、ASR、TTS；每個 origin 都要各自具備開機啟動設定。

### 設定與驗證開機恢復

1. `cloudflared`：第 2 節的 `sudo systemctl enable --now cloudflared` 已同時設定開機啟動並立即啟動 connector。
2. LLM：依 [`docs/llama.md`](llama.md) 的 `llama-4b.service` 段落建立 unit；它使用 `127.0.0.1:8000`、`network-online.target`、`Restart=always`，並以 `enable --now` 啟用。
3. ASR／TTS 若由 Docker 執行，先查看實際 container name：

```bash
sudo docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
```

把下列兩個變數的值替換成上方 `NAMES` 欄顯示的實際名稱，再設定 container 自動恢復：

```bash
ASR_CONTAINER='replace-with-asr-container-name'
TTS_CONTAINER='replace-with-tts-container-name'

sudo docker update --restart unless-stopped "$ASR_CONTAINER" "$TTS_CONTAINER"
sudo systemctl enable --now docker
sudo docker inspect --format '{{.Name}} restart={{.HostConfig.RestartPolicy.Name}}' \
  "$ASR_CONTAINER" "$TTS_CONTAINER"
```

驗證輸出中的兩個 container 都是 `restart=unless-stopped`（或已採用的 `always`）。只啟用 `docker.service` 不代表既有 container 一定會在開機時啟動。若 ASR／TTS 不是 Docker，請在其實際 service manager 設定等效的 `enable` 與失敗自動重啟。

若使用 Docker Compose，對應 service 至少要有：

```yaml
services:
  asr:
    restart: unless-stopped
    ports:
      - "127.0.0.1:9000:9000"
  tts:
    restart: unless-stopped
    ports:
      - "127.0.0.1:5000:5000"
```

保留既有 image、command 與其他設定；只確認 `restart` 與 port publish 的 host bind address 符合本文件的存取邊界。

最後檢查：

```bash
systemctl is-enabled cloudflared
systemctl is-enabled llama-4b
systemctl is-active cloudflared llama-4b
sudo docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
sudo ss -ltnp | grep -E ':(5000|8000|9000)\b'
```

應符合：`cloudflared` 與 `llama-4b` 都是 `enabled`／`active`；ASR／TTS container 的 restart policy 是 `unless-stopped` 或 `always`；只走 Cloudflare 時三個 model port 的監聽位址是 `127.0.0.1`。只要 origin 尚未 ready，Tunnel 可能短暫回 `502`；origin 恢復後 connector 會重新轉送，不需要重建 Tunnel。

### 是否只能透過 Cloudflare

目前設定的 public hostname 會經 Cloudflare Access；但 Tunnel **不會自動封鎖 origin 的直接 port**：

| 連線方式 | 是否經 Access |
| --- | --- |
| `https://llm.yizhe.dev`／`asr.yizhe.dev`／`tts.yizhe.dev` | 是，需 Service Token |
| 模型主機上的 `localhost:8000/9000/5000` | 否，這是本機 bypass |
| 模型主機 IP 的 `:8000`／`:9000`／`:5000` | 否；若服務綁 `0.0.0.0` 且防火牆放行即可直連 |

要讓外部只能走 Cloudflare：

1. LLM 綁 `127.0.0.1`；[`docs/llama.md`](llama.md) 的 4B 範例已使用此設定。只有確實需要 LAN 直連時才改為 `0.0.0.0`，並自行設定防火牆；`cloudflared` 同機即可連線。
2. Docker ASR／TTS 的 port publish 綁 `127.0.0.1`，不要 publish 到 `0.0.0.0`。
3. 防火牆封鎖外部到 TCP `5000`、`8000`、`9000`；SSH `22` 也可只允許管理網段。修改 SSH 防火牆前先用第二個 Cloudflare SSH session 測試，避免把自己鎖在主機外。
4. 從外部只使用三個 HTTPS hostname；後端的 `LLM_BASE_URL`、`ASR_BASE_URL`、`TTS_BASE_URL` 保持使用上述 public hostname。

本機程式或取得主機 shell 的使用者仍可直接呼叫 `localhost`；Cloudflare Access 只保護公開 hostname，不是本機權限控制。

## 故障排除

| 現象 | 檢查 |
| --- | --- |
| 未帶 Token 回 `200` | Access application hostname 或 policy 未套用；停止使用該 endpoint，先修正 Access。 |
| 未帶 Token 回 `302` | Policy 使用互動式登入；模型 application 只保留 `Service Auth`。 |
| 帶 Token 回 `403` | Client ID／Secret 是否為同一個 Service Token；policy 是否選到正確 token；`Protect with Access` 的 team name／AUD 是否對應該 application。 |
| `include field should not be empty` | Value 欄只輸入文字，未點選下拉選項。 |
| `502 Bad Gateway` | Tunnel 已連線但 origin port 錯誤、服務未啟動或服務只綁到錯誤介面。 |
| Tunnel 顯示 disconnected | `systemctl status cloudflared`、connector token、主機網路。 |
| DNS `NXDOMAIN` | Public hostname 尚未儲存、domain 選錯或 DNS 記錄未建立。 |
| 後端報 Service Token 必須同時設定 | 同時設定 `CF_ACCESS_CLIENT_ID` 與 `CF_ACCESS_CLIENT_SECRET`。 |
| LLM `404` | `LLM_BASE_URL` 必須包含 `/v1`；模型列表使用 `${LLM_BASE_URL}/models`。 |
| TTS 回 JSON 而非 WAV | 查看 HTTP status 與 response body；確認 model、voice、endpoint。 |
| ASR 回 200 但辨識錯誤 | 播放 WAV，確認 TTS 輸出，再檢查取樣率、ASR 模型與語言資料。 |
| SSH 無法連線 | 確認 SSH route、SSH Access application、用戶端 `cloudflared` 與登入 policy。 |
| `uv: command not found` | 先安裝 README「前置需求」中的 `uv`。 |
| 後端 port `8000` 被占用 | 同機執行時改用其他 port；模型主機的 `8000` 保留給 LLM。 |
