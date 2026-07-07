# ASR 短指令

> **⚠️ DEPRECATED**: 此文件描述的 REST ASR 架構與前端 VAD 實作，已被 **Pipecat WebRTC 架構** 取代。
> 最新語音架構與實作狀態，請參閱 [`pipecat_webrtc_plan.md`](./pipecat_webrtc_plan.md)。
## 目標

ASR 只支援固定站牌場景的短指令輔助，例如「二○一路」、「往斗六」、「還有車無」。不承擔開放式目的地理解。

## 狀態

大致完成。後端 proxy 與前端錄音流程已完成；剩下 mic 拒絕引導。

## 已完成

- [x] `POST /api/asr` proxy：FastAPI 收 multipart audio，轉送 Qwen3-ASR endpoint
- [x] Env：`ASR_BASE_URL`、`ASR_MODEL`、`ASR_API_KEY`
- [x] 錯誤映射：timeout / 5xx / 空白 transcription
- [x] 可觀測性：FastAPI、HTTPX upstream、`pipeline.asr.audio_bytes`
- [x] 麥克風擷取：`getUserMedia` + echo cancellation / noise suppression / auto gain
- [x] `MediaRecorder` webm/opus 後轉 16 kHz WAV
- [x] energy-based VAD auto-stop
- [x] 錄音中 pulse ring、音量 bar、processing spinner
- [x] 轉文字後填入 textarea，由使用者確認再送

## 待辦

- [x] Mic permission 拒絕引導：`micDenied` state，按鈕變灰 + MicOff 圖示 + 說明文字，disabled 不重試

## 決策

- 不用 LiveKit / WebRTC：Kiosk 是單一使用者、固定網路、turn-based 互動。
- VAD 放前端：少一趟 RTT，不傳沒講話時的雜訊。
- 轉文字後先 confirm：防 ASR 錯字，給使用者修正機會。

## 相關文件

- `docs/product-positioning.md`
- `tasks/station-attendant.md`
