# ASR 短指令

## 目標

ASR 只支援固定站牌場景的短指令輔助，例如「唸給我聽」、「二○一路」、「往斗六」、「還有車無」。不承擔開放式目的地理解。

## 狀態

In progress。後端 proxy 與前端錄音流程已完成；剩下 permission 引導、短指令資料集與評估。

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

- [ ] Mic permission 拒絕引導頁
- [ ] 短指令 intent set：唸給我聽、查路線、重新整理、停止播放、需要協助
- [ ] 有限詞彙 ASR 評估：路線號、方向、常用短語
- [ ] 中文 ASR / 台語 ASR / 混合輸入比較
- [ ] 地名 hotword injection：雲林站名 + 路線號
- [ ] 數字 / 字母路線輸出格式測試集

## 評估

- [ ] capture-end -> text-return p50 / p95
- [ ] 自建短指令 test set，至少 100 句
- [ ] WER / CER on 台語與華語混合 test set
- [ ] 路線號 / 方向 / 指令 intent recall
- [ ] ASR 失敗後 fallback 完成率

## 決策

- 不用 LiveKit / WebRTC：Kiosk 是單一使用者、固定網路、turn-based 互動。
- VAD 放前端：少一趟 RTT，不傳沒講話時的雜訊。
- 轉文字後先 confirm：防 ASR 錯字，給使用者修正機會。
- 錄音預設不存；評估期若需暫存，必須先處理同意書、保留期限與遮罩策略。

## 相關文件

- `docs/product-positioning.md`
- `tasks/station-attendant.md`
- `tasks/evaluation.md`
