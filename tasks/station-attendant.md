# 數位站務員

## 目標

虛擬人定位為數位站務員：用台語播報離站決策，並和畫面 route card highlight 同步。它不是自由聊天主體。

## 狀態

In progress。核心互動流程已完成；視覺狀態機等待組員提供 3D 模型 API。

## 已完成

- [x] PIP 虛擬站務員子母畫面：4:5 肖像、4 角位置、3 尺寸、移動模式、對話面板
- [x] PIP 對話接 `/api/chat` 後端
- [x] Hero 操作按鈕：「需要幫忙嗎？讓小芸幫您」
- [x] 開啟 PIP 時自動問候「請問您欲前往哪裡？」並 TTS 播放
- [x] `ttsState` / `mouthAmplitude` 說話狀態已從 `useTts` 取出
- [x] `isSending` 追蹤 thinking 狀態
- [x] TTS 播放 + 字幕 typewriter 動畫同步（`speakWithAnimation`）
- [x] ASR 語音輸入接 `sendVoiceMessage`

## 待辦

- [ ] **站務員視覺狀態機（idle/speaking/listening/thinking/error）**：blocked，等待組員提供 3D 模型與狀態切換 API

## 驗收

- [ ] 首頁摘要播報反映真實離站資料
- [ ] 播報期間 route card highlight 同步
- [ ] ASR 失敗時有明確 fallback 引導

## 相關文件

- `docs/product-positioning.md`
- `tasks/tts.md`
- `tasks/asr.md`
- `tasks/departure-dashboard.md`
