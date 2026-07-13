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

## 2026-07-12 播放同步字幕做反：段「播畢」當成「起播」
- 症狀：字幕在語音播完該段才成批冒出，體感嚴重落後
- 根因：pipecat 預設 TTSTextFrame 排在該段音訊「之後」進實時佇列；查證回報寫「音訊已送出播放」，被解讀成起播，實為播畢
- 規則：任何「與播放同步」的設計，規格必須明寫同步點是「起播」還是「播畢」，並在佇列順序層面確認 frame 在音訊前/後；相對時序類機制上線前先實測體感
- 證據：commit 91b5d0a（SubtitleFrame 改排音訊前 + durationMs 逐字揭示）

## 2026-07-13 subagent 起背景長跑後停 turn 等不到通知（兩次踩雷）
- 症狀：subagent 用 run_in_background 起 eval/批次腳本後結束 turn，回報「等通知後繼續」，實際永遠不會被叫醒，任務停在半路
- 根因：subagent 的背景 bash 完成通知不可靠（或其 Monitor 語意與主線不同），停 turn = 任務死掉，要靠指揮官手動 SendMessage 推
- 規則：派長跑任務（eval、批次、多分鐘腳本）的 prompt 必須寫死「同步輪詢到完成再回報，不要結束 turn 等背景通知」；指揮官收到「等通知中」的中間回報一律立刻推一次
- 證據：2026-07-12 ASR eval agent 停在 13/20、2026-07-13 eval 重跑 agent 停在剛起跑，皆需 SendMessage 推才完成
