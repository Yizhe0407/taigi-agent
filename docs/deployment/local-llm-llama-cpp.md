# 用 llama.cpp 部署 LLM

以 llama.cpp 執行量化的 Qwen3.5-4B，提供 OpenAI 相容 API 給後端使用。

選擇 llama.cpp 而非 vLLM 的原因：它是單一執行檔，不會遇到 Python / CUDA 編譯衝突；顯存依需求配置，可與其他服務共用同一張 GPU。

使用環境：RTX 4000 Ada（20 GB），Qwen3.5-4B Q8_0，context 8K。

## 安裝與啟動

```bash
curl -LsSf https://llama.app/install.sh | sh   # 裝好後 llama --version 應有回應
```

```bash
llama serve -hf unsloth/Qwen3.5-4B-GGUF:Q8_0 \
  --jinja --host 127.0.0.1 --port 8000 \
  -ngl 99 -fa on -c 8192 --temp 0 \
  --chat-template-kwargs '{"enable_thinking": false}'
```

第一次會自動從 Hugging Face 下載模型。確認有起來：

```bash
curl http://127.0.0.1:8000/v1/models    # 應看到 unsloth/Qwen3.5-4B-GGUF:Q8_0
```

後端 `.env` 設 `LLM_BASE_URL=http://127.0.0.1:8000/v1`。要讓別台機器透過 Cloudflare 使用，看 [模型服務設定](model-services-cloudflare.md)。

## 參數為什麼這樣設

| 參數 | 為什麼 |
|---|---|
| `--jinja` | **一定要開**。不開的話工具呼叫會變成純文字漏進回答，而且不報錯 |
| `--temp 0` | 工具呼叫的輸出才穩定 |
| `enable_thinking: false` | 關掉思考模式，回答和工具串接比較乾淨 |
| `-fa on` | Flash Attention。只寫 `-fa` 會語法錯誤，要寫 `on` |
| `-ngl 99` | 全部層都放進顯存 |
| `--host 127.0.0.1` | 只給本機和 Cloudflare Tunnel 用。真的要 LAN 直連才改 `0.0.0.0`，並自己設防火牆 |

## 開機自動啟動

用**非 root** 帳號執行。已經有 `llama-4b` unit 的話腳本會停下來，不會覆寫。

```bash
SERVICE_USER="$(id -un)"
[ "$SERVICE_USER" = root ] && { echo "請用非 root 帳號" >&2; exit 1; }
if sudo systemctl cat llama-4b >/dev/null 2>&1; then
  echo "已有 llama-4b unit，未覆寫：" >&2; sudo systemctl cat llama-4b; exit 1
fi

SERVICE_HOME="$(getent passwd "$SERVICE_USER" | cut -d: -f6)"
LLAMA_BIN="$(sudo -u "$SERVICE_USER" -H sh -lc 'command -v llama')"
[ -n "$SERVICE_HOME" ] && [ "${LLAMA_BIN#/}" != "$LLAMA_BIN" ] || { echo "找不到 llama 路徑" >&2; exit 1; }

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
sudo systemctl is-active llama-4b
```

## 換別的模型

換掉 `-hf` 後面的 Hugging Face 倉庫即可，其他參數意義不變。
