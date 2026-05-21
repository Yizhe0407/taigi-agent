# taigi-bus-agent

雲林公車台語語音助理（大學專題）。

以 **agent harness** 為架構核心，讓使用者用台語詢問雲林縣公車資訊。
目前為打字 CLI 模式，後期接語音（ASR + TTS）。

## 架構概念

```
使用者輸入
    ↓
agent loop（harness 核心）
    ├─ LLM 決定要呼叫哪個工具
    ├─ 執行工具（TDX API / OTP）
    └─ LLM 組成自然語言回答
    ↓
使用者看到答案
```

參考架構：[learn-claude-code](https://github.com/shareAI-lab/learn-claude-code)

## 場域

PoC 場域：斗六市為核心，延伸至虎尾、北港、西螺、麥寮。

| 路線 | 區間 |
|------|------|
| 7126 | 斗六－北港 |
| 7720 | 斗六－虎尾 |
| 7700 | 斗六－西螺 |
| 7124 | 斗六－麥寮 |

## 前置需求

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- OpenAI-compatible LLM API（Ollama 或 vLLM）
- TDX API 帳號（[申請](https://tdx.transportdata.tw/)）

### vLLM 啟動參數（若使用 vLLM）

tool calling 與非思考模式需要額外參數：

```bash
vllm serve Qwen/Qwen3-4B-Instruct-2507 \
  --enable-auto-tool-choice --tool-call-parser hermes \
  --reasoning-parser qwen3
```

## 安裝與執行

```bash
# 1. 安裝依賴
uv sync

# 2. 設定環境變數
cp .env.example .env
# 必填：LLM_BASE_URL、LLM_MODEL、TDX_CLIENT_ID、TDX_CLIENT_SECRET
# Kiosk 設定：KIOSK_STOP（這台機器在哪個站牌，預設「雲林科技大學」）
# 選填：KIOSK_DIRECTION=去程 或 回程（不填 = 顯示兩個方向）

# 3. 啟動
uv run python main.py
```

## 目前支援功能

系統為 Kiosk 模式，部署在固定站牌，查詢「這站」的到站資訊。

| 問法 | 工具 | 資料來源 |
|------|------|----------|
| 「201 幾分鐘到」 | `get_arrivals_here` | ebus.yunlin.gov.tw |
| 「7126 下一班幾分鐘到」 | `get_arrivals_here` | TDX InterCity |
| 「7126 今天幾點有車」 | `get_schedule` | TDX |
| 「201 停哪些站」 | `get_route_stops` | ebus（從到站資料重組） |
| 「7126 停哪些站」 | `get_route_stops` | TDX StopOfRoute |
| 「這站有哪些路線」 | `get_routes_at_stop` | ebus.yunlin.gov.tw |

不支援：行程規劃、換乘建議、站間行駛時間。

## 舊專案

架構重寫前的版本（含 LiveKit 語音、Admin 後台）：`/Users/yizhe/Developer/taigi-flow`（唯讀參考）
