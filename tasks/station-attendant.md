# 數位站務員

## 目標

虛擬人定位為數位站務員：用台語播報離站決策，並和畫面 route card highlight 同步。它不是自由聊天主體。

## 狀態

Not started / design ready。PIP 對話外殼已存在，但站務員腳本、狀態機、字幕與 highlight 尚未完成。

## 已完成

- [x] PIP 虛擬站務員子母畫面：4:5 肖像、4 角位置、3 尺寸、移動模式、對話面板
- [x] PIP 對話接 `/api/chat` 後端
- [x] Hero 操作按鈕：「需要幫忙嗎？讓小芸幫您」

## 待辦

- [ ] 首頁摘要腳本：目前可搭、未發車、末班已過
- [ ] 單一路線播報腳本
- [ ] 資料錯誤與 ASR 失敗 fallback 腳本
- [ ] 站務員狀態機：idle、speaking、listening、thinking、error、offline
- [ ] 從 structured departure snapshot 產生短播報句
- [ ] 播報同步 highlight：唸到哪一路線，畫面對應 route card 高亮
- [ ] 字幕區：台語 / 華語輔助文字
- [ ] TTS chunk 播放與站務員狀態同步
- [ ] 虛擬人缺省模式：avatar 或渲染失敗時退回靜態站務員面板
- [ ] 效能預算：avatar 不可影響到站資訊刷新與主要互動延遲

## 驗收

- [ ] 可主動播報「目前可搭」情境
- [ ] 可主動播報「末班已過」情境
- [ ] 播報期間 route card highlight 與字幕同步
- [ ] ASR 失敗時引導使用者改按大按鈕，不重複要求自由說話

## 相關文件

- `docs/product-positioning.md`
- `tasks/tts.md`
- `tasks/asr.md`
- `tasks/departure-dashboard.md`
