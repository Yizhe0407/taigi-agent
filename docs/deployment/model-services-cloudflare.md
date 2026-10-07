# 模型服務設定（Cloudflare Tunnel + Access）

LLM、ASR（語音辨識）、TTS（語音合成）跑在學校的 GPU 主機上，後端在另一台機器。後端透過 Cloudflare Tunnel 連到模型主機，並用 Cloudflare Access 的 Service Token 驗證，模型服務不直接對外開放：

```
後端 ──HTTPS + Service Token──▶ Cloudflare ──Tunnel──▶ 模型主機（只聽 127.0.0.1）
                                  │
                                  └─ 沒帶 Token 的請求，在這裡就被擋掉
```

| 服務 | 對外網址 | 模型主機上的 port |
|---|---|---|
| LLM | `llm.yizhe.dev` | 8000 |
| ASR | `asr.yizhe.dev` | 9000 |
| TTS | `tts.yizhe.dev` | 5000 |

三個服務共用同一條 Tunnel 和同一個 Service Token。

## 步驟

每一步的完整指令和驗證都在 [逐步設定手冊](model-services-cloudflare-runbook.md)：

1. 在模型主機確認三個服務都有回應（手冊第 1 節）
2. 確認 `cloudflared` 連線正常（第 2 節）
3. 在 Cloudflare 建 Service Token、policy、Access application（第 3–5 節）
4. 加三個 public hostname（第 6 節）
5. 設定後端（見下）
6. 從外部驗證：沒帶 Token 會被擋、帶了才通（第 8–13 節）
7. 設定開機自動啟動（第 17 節）

## 設定後端

在 `backend/.env` 填：

```dotenv
LLM_BASE_URL=https://llm.yizhe.dev/v1
LLM_MODEL=unsloth/Qwen3.5-4B-GGUF:Q8_0
ASR_BASE_URL=https://asr.yizhe.dev
ASR_MODEL=breeze-asr-26
TTS_BASE_URL=https://tts.yizhe.dev
TTS_MODEL=taigi_text_epoch3419
TTS_VOICE=taigi_text_epoch3419
CF_ACCESS_CLIENT_ID=<Service Token 的 Client ID>
CF_ACCESS_CLIENT_SECRET=<Service Token 的 Client Secret>
```

後端會自動把兩個 `CF-Access-*` header 加到每個模型請求。只填其中一個，後端會直接報錯，不會送出沒驗證完整的請求。

## 容易誤會的地方

Access 只保護公開網址。在模型主機上直接打 `localhost:8000`，或對方知道主機 IP 且 port 對外開放，都不經過 Access。

所以要做到「只能走 Cloudflare」，必須：

- 三個服務都綁 `127.0.0.1`，不要綁 `0.0.0.0`
- 防火牆擋掉外部到 5000、8000、9000

## 出問題時

| 現象 | 通常是 |
|---|---|
| 沒帶 Token 卻回 200 | Access 沒套到這個網址，先停用該 endpoint 再修 |
| 帶了 Token 回 403 | Client ID 和 Secret 不是同一組，或 policy 選錯 token |
| 502 Bad Gateway | Tunnel 通了，但模型服務沒開或 port 錯 |
| LLM 回 404 | `LLM_BASE_URL` 少了結尾的 `/v1` |
| Tunnel 顯示 disconnected | `systemctl status cloudflared` |

更多排錯見手冊最後的「故障排除」。
