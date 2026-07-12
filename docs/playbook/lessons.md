# 踩雷教訓帳本

> 格式與整理規則見 `maintenance.md`。新教訓往下追加。

## 2026-07-04 Vite cache 誤入 git staging
- 症狀：`frontend/.vite/deps/` 兩個 cache 檔被 staged
- 根因：`.gitignore` 沒擋 `.vite/`，`git add` 範圍過寬
- 規則：commit 前跑 `git status --short`，出現非手寫新檔（cache/build 產物）先查 `.gitignore` 再 commit
- 證據：frontend/.gitignore 已加 `.vite/`（2026-07-04）

## 2026-07-04 計劃檔無狀態區塊，續作要重推導
- 症狀：pipecat 計劃 Phase 2 完成與否要靠 git status 反推
- 根因：`tasks/*.md` 只有設計、沒有「現在做到哪」
- 規則：進行中計劃檔頂部維護「## 目前狀態」≤5 行，每完成一步立即更新
- 證據：tasks/pipecat_webrtc_plan.md（2026-07-04 時的狀態）

## 2026-07-12 宣稱串流上線但體感沒變快
- 症狀：LLM→TTS 改串流後，使用者實測首音仍等很久、chat 看不到逐字
- 根因：兩層各自的聚合器互相憋——自家 StreamNormalizer 等句號才放行（站名清單整句無句號），Pipecat TTSService 預設 SENTENCE 模式又把 chunk 重新緩衝到句尾
- 規則：宣稱延遲改善前必須 live probe 量 chunk 到達時間（不能只看單元測試綠）；串流管線改造要檢查下游每一層有沒有自己的緩衝/聚合
- 證據：commit 0cc337c，probe 由 7.2s 一塊 → 5.8s 起 10 塊

## 2026-07-12 前端 idle timer 把播音期當閒置
- 症狀：語音長回覆播放中，對話被 30s/60s timer 自動關閉
- 根因：timer 只認 data channel 文字事件為「活動」，TTS 音訊走 media track 完全不觸發
- 規則：設計 idle/watchdog timer 時先列舉「所有算活動的訊號」，音訊/串流這類非請求式通道要有顯式心跳（bot_speaking/bot_silent）
- 證據：commit 609e54b
