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

執行下列指令。llama.cpp 會自動處理 Hugging Face 模型下載並載入至 GPU：

```bash
llama serve -hf unsloth/Qwen3.5-4B-GGUF:Q8_0 \
  --jinja \
  --host 0.0.0.0 --port 8000 \
  -ngl 99 \
  -fa on \
  -c 8192 \
  --temp 0 \
  --chat-template-kwargs '{"enable_thinking": false}'
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
