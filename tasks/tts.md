# TTS 台語播報

> **⚠️ DEPRECATED**: 此文件描述的 REST TTS 架構（由前端 fetch + Web Audio API 排序播放），已被 **Pipecat WebRTC 架構** 與後端串流 TTS 取代（僅保留文字模式 fallback）。
> 最新語音架構與實作狀態，請參閱 [`pipecat_webrtc_plan.md`](./pipecat_webrtc_plan.md)。
## 目標

把結構化離站決策轉成可聽懂的台語播報，降低閱讀負擔。

## 狀態

大致完成。proxy、pipeline、前端播放、字幕動畫均已完成；剩下播報文案與 MOS 評分。

## 已完成

- [x] `POST /api/tts` proxy
- [x] HanloFlow -> Taibun -> Piper TTS pipeline
- [x] TTS text normalization：路線號逐位、時刻按時間結構、分鐘量詞
- [x] 前端分段播放：`splitIntoChunks` -> 平行 fetch -> Web Audio API 順序 schedule
- [x] `AudioContext.resume()` 放在 user gesture 內，處理瀏覽器 autoplay policy
- [x] 可觀測性：`tts.text_process` span、`pipeline.stage.duration`
- [x] TTS 狀態同步到 PipFrame：`ttsState` + `mouthAmplitude` 已傳至 `Live2DAvatar`
- [x] 字幕區：`lastAgentText` bubble 在 PipFrame 顯示，typewriter 動畫與 TTS 同步
- [x] thinking 狀態：`isSending || ttsState === 'loading'` → 跳點 + 「小芸思考中…」

## 待辦

- [ ] TTS 自然度 MOS 評分（報告素材）

## 驗收

- [x] 播報不中斷 departure polling
- [x] 播報失敗時 UI 能清楚回到可操作狀態

## 相關文件

- `docs/observability.md`
- `tasks/station-attendant.md`
