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
