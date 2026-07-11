# 給未來 Session 的信

> 寫於 2026-07-04，Fable 5 唯一一次 session。你（讀者）大概是 Sonnet 或 Haiku。這封信是判斷的殘留，不是規則——規則在其他 playbook 檔。

## 三件沒被問、但最重要的事

**1. 你的瓶頸是遺失狀態，不是智力不夠。**
弱模型失敗的主因是 context 斷掉後重建錯誤，不是推理不動。所以這套制度全部押在「狀態外部化」：tasks/*.md 的目前狀態區塊、lessons.md、立即存檔。你覺得「等做完再更新文件」的那一刻，就是制度失效的起點。先寫狀態，再繼續做。

**2. 這個 repo 的測試是你的義肢。**
backend 有 22 個測試檔，品質底線 = `uv run pytest` 綠。你對自己 diff 的信心不值錢，測試綠才值錢。改任何邏輯，寧可多花 token 補一個測試，也不要多花 token 在回報裡解釋「為什麼我覺得對」。沒測試覆蓋的區域（frontend 互動、voice pipeline 實時行為）是你最容易出貨壞東西的地方——那裡要用實跑驗證（`/verify`、實際起 server）。

**3. 「假完成」是你最大的職業風險。**
弱模型最常見的失敗不是做錯，是宣稱做完但沒驗。回報「卡住了、因為 X」的價值遠高於回報「完成」然後被抓包。judgment.md 第 2 節的完成定義是硬的：缺一項就不准用「完成」這個詞。誠實的阻塞回報不會被扣分，假完成會摧毀使用者對整套制度的信任。

## 制度退化分析

最可能的退化路徑，依機率排序：

1. **制度被略過（最可能）**：趕任務的 session 直接動手，playbook 從沒被讀。防線只有一條：CLAUDE.md 的「開場協議」3 行常載。若發現自己已經做了三步還沒讀 dispatch——停下，讀，再繼續。使用者可偶爾抽查：「你派工前讀了 model-dispatch 嗎？」
2. **lessons.md 變垃圾場**：什麼都往裡寫、從不整理 → 讀的成本超過價值 → 沒人讀。防：maintenance.md 的整理觸發條件（>15 則或 100 行），以及「repo 查得到的不寫」門檻。
3. **CLAUDE.md 膨脹**：每個 session 都想加一條，兩個月後 300 行 → 常載 token 漏。防：150 行硬上限，先刪再加。
4. **制度檔被「好心」修壞**：弱模型覺得規則不合理就改。防：maintenance.md 權限表——制度檔唯讀，想改就問使用者。

## 信心最低的產出（誠實列出）

1. **model-dispatch.md 的 effort 細節**：來自 subagent 轉述官方文件（部分型號名我未親眼驗證）。文件會過時，引用前重查 code.claude.com/docs。
2. **升降級規則的數字**（錯一次升級、兩輪換路）：使用者給的框架合理，但數字沒有實驗支撐。用兩週後依實感校準——這是少數「可自行提案修改制度」的正當理由，仍需問過使用者。
3. **diagnosis.md 的前三名排序**：樣本只有一次環境盤點＋一個進行中計劃，排序是判斷不是測量。
4. **無法確認的**：本 session 是否被導向 Opus 4.8、額度實際消耗——session 內部無法驗證，建議 usage 儀表板實測。

## 刻意沒做的（不是遺漏）

- **沒建 `.claude/agents/` custom agents**：prompts.md 範本＋Agent tool 的 model 參數已夠用；custom agent 是多一層要維護的東西，等範本被用出真實痛點再建。
- **沒動 `~/.claude/CLAUDE.md`**：使用者私人檔。它的思考框架與 JSDoc 策略和本制度不衝突。
- **沒把 playbook 做成 @-import**：`@` 會每 session 自動載入，違反常載預算。索引是純路徑，按需讀。

## 未完成交接：pipecat WebRTC 計劃

制度建立 session 不執行日常任務，此為狀態快照（2026-07-04）：

- Phase 1–4 完成（custom services、pipeline+barge-in、前端 useWebRTC 遷移、REST voice 清理）。
- **Phase 5 進行中**，未完項目：平板 AEC 實測、barge-in 延遲驗證（目標 <200ms）、斷線重連 fallback、telemetry 整合、`docs/architecture.md` voice pipeline 段落更新（已部分修改）。
- Working tree 有約 30 個未 commit 檔案，全屬此計劃。下個 session：先讀 `tasks/pipecat_webrtc_plan.md` 頂部「目前狀態」，考慮先把 Phase 1–4 commit 掉再繼續 Phase 5。
