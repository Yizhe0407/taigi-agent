# TTS 台語播報

## 目標

把結構化離站決策轉成可聽懂的台語播報，降低閱讀負擔。TTS 的價值是協助使用者做出正確等車決策，不只是把文字唸出來。

## 狀態

In progress。TTS proxy、文字轉換與前端分段播放已完成；剩下和數位站務員狀態、字幕、highlight 的整合。

## 已完成

- [x] `POST /api/tts` proxy
- [x] HanloFlow -> Taibun -> Piper TTS pipeline
- [x] TTS text normalization：路線號逐位、時刻按時間結構、分鐘量詞
- [x] 前端分段播放：`splitIntoChunks` -> 平行 fetch -> Web Audio API 順序 schedule
- [x] `AudioContext.resume()` 放在 user gesture 內，處理瀏覽器 autoplay policy
- [x] 可觀測性：`tts.text_process` span、`pipeline.stage.duration`

## 已完成（補記）

- [x] TTS 狀態同步到 PipFrame：`ttsState` + `mouthAmplitude` 已傳至 `Live2DAvatar`
- [x] 字幕區：`lastAgentText` bubble 在 PipFrame 顯示，typewriter 動畫與 TTS 同步
- [x] thinking 狀態：`isSending || ttsState === 'loading'` → 跳點 + 「小芸思考中…」

## 待辦

- [ ] 首頁摘要播報文案（按鈕後先播報離站狀態再問「要去哪？」）
- [ ] 單一路線播報文案
- [ ] 末班已過 / 資料錯誤播報文案
- [ ] route card highlight 與 TTS 播報同步
- [ ] First-audio latency 量測
- [ ] TTS 自然度 MOS 評分

## 驗收

- [ ] 按鈕後能播報本站重點狀態
- [ ] 播報不中斷 departure polling
- [ ] 播報失敗時 UI 能清楚回到可操作狀態

## 相關文件

- `docs/observability.md`
- `tasks/station-attendant.md`
- `tasks/evaluation.md`
