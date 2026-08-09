# 使用 llama.cpp 部署本地 LLM

本文說明如何在本機 GPU 上，以 llama.cpp 部署量化後的 Qwen3.5-4B，並對外提供
OpenAI 相容的 API 伺服器，供本專案的 agent 作為 LLM 後端使用。

相較於 vLLM 對本地環境、GPU 驅動與 `flashinfer` / `triton` 的嚴苛依賴，llama.cpp
以純 C/C++ 核心運作，可避免 Python 與 CUDA 的編譯衝突；其顯存為動態分配，不會
預先霸佔剩餘空間，適合與其他服務共用同一張 GPU。

## 環境與硬體

- 硬體：NVIDIA RTX 4000 Ada Generation（20 GB VRAM）
- 模型：Qwen3.5-4B-Instruct，8K context，Q8_0 量化
- 相依：Conda（用於隔離 Python 環境）、可連外下載模型的網路

## 部署步驟

### 一、建立並啟用 Conda 環境

建立一個乾淨、隔離的 Python 環境（此處採用 Python 3.10）：

```bash
conda create -n llama-env python=3.10 -y
conda activate llama-env
```

### 二、安裝 llama 工具鏈

使用官方整合的安裝腳本，取得單一執行檔並設定全域 `llama` 指令（含 CUDA 加速）。
此方式不經由 Python 套件封裝，可避免相依衝突：

```bash
curl -LsSf https://llama.app/install.sh | sh
```

安裝完成後，執行 `llama --version` 確認可正常回應。

### 三、啟動 OpenAI 相容 API 伺服器

#### 4B（本 Cloudflare 流程使用）

[`cloudflare-model-services.md`](cloudflare-model-services.md) 的 Cloudflare Tunnel + Access
流程使用下列 4B 啟動命令。llama.cpp 會自動處理 Hugging Face 模型下載並載入至 GPU：

```bash
llama serve -hf unsloth/Qwen3.5-4B-GGUF:Q8_0 \
  --jinja \
  --host 127.0.0.1 --port 8000 \
  -ngl 99 \
  -fa on \
  -c 8192 \
  --temp 0 \
  --chat-template-kwargs '{"enable_thinking": false}'
```

只走 Cloudflare Tunnel 時綁定 `127.0.0.1`；若確實需要 LAN 直連，才改回 `0.0.0.0`，並自行設定防火牆保護。

#### 4B systemd 開機自動啟動與崩潰重啟

以下段落請以**非 root 的 service account** 執行；它會從該帳號的 `command -v llama` 取得絕對路徑。若已有 `llama-4b` unit，**不可直接覆寫**，先用 `sudo systemctl cat llama-4b` 確認目前設定是否等效，再決定是否調整：

```bash
SERVICE_USER="$(id -un)"
if [ "$SERVICE_USER" = root ]; then
  echo "請以非 root 的 service account 執行此段落" >&2
  exit 1
fi

if sudo systemctl cat llama-4b >/dev/null 2>&1; then
  echo "已有 llama-4b unit；請先確認目前設定，未覆寫任何檔案：" >&2
  sudo systemctl cat llama-4b
  exit 1
fi

SERVICE_HOME="$(getent passwd "$SERVICE_USER" | cut -d: -f6)"
LLAMA_BIN="$(sudo -u "$SERVICE_USER" -H sh -lc 'command -v llama')"
if [ -z "$SERVICE_HOME" ] || [ -z "$LLAMA_BIN" ] || [ "${LLAMA_BIN#/}" = "$LLAMA_BIN" ]; then
  echo "找不到 service account 的 llama 絕對路徑或家目錄" >&2
  exit 1
fi

sudo tee /etc/systemd/system/llama-4b.service >/dev/null <<EOF
[Unit]
Description=Qwen3.5-4B llama.cpp API server
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
Environment="HOME=$SERVICE_HOME"
ExecStart=$LLAMA_BIN serve -hf unsloth/Qwen3.5-4B-GGUF:Q8_0 --jinja --host 127.0.0.1 --port 8000 -ngl 99 -fa on -c 8192 --temp 0 --chat-template-kwargs '{"enable_thinking": false}'
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now llama-4b
sudo systemctl is-enabled llama-4b
sudo systemctl is-active llama-4b
sudo journalctl -u llama-4b -n 50 --no-pager
```

#### 9B（替代方案）

下列 9B 啟動命令僅為替代方案，不是上述 Cloudflare 流程使用的預設模型：

```bash
llama serve -hf unsloth/Qwen3.5-9B-GGUF:IQ4_NL \
  --no-mmproj \
  --jinja \
  --host 0.0.0.0 --port 8000 \
  -ngl 99 \
  -fa on \
  -c 8192 \
  --parallel 1 \
  --temp 0 \
  --reasoning off
```

## 參數說明

| 參數 | 說明 |
| --- | --- |
| `-hf unsloth/...:Q8_0` | 指定 Hugging Face 倉庫與量化版本。首次執行會自動下載至 `~/.cache/` 並載入，無須手動下載。 |
| `--jinja` | 啟用模型的 chat template 與 tool-call 解析。工具呼叫（function calling）必須開啟此旗標，否則工具呼叫會以純文字形式漏入回應內容，且不會報錯。 |
| `-ngl 99` | GPU 卸載層數（number of gpu layers）。設為足夠大的值以確保所有模型層皆載入顯存，達到全速推理。 |
| `-fa on` | Flash Attention。新版 `llama` 需明確指定 `on`（或 `auto`），僅寫 `-fa` 會產生語法錯誤。 |
| `-c 8192` | 上下文長度（context size）。 |
| `--temp 0` | 取樣溫度設為 0，使工具呼叫的輸出趨於確定，提高可靠度。 |
| `--chat-template-kwargs '{"enable_thinking": false}'` | 關閉思考模式，跳過推理模型的思維鏈輸出，使一般對話與工具呼叫的串接更乾淨。 |

## 適用範圍

此 `llama serve -hf` 工作流適用於 Hugging Face 上的任何 GGUF 模型；替換 `-hf`
後方的倉庫與模型名稱即可部署其他模型（如 Llama-3、Gemma 等），其餘參數的意義
不變。
