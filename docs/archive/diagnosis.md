# Harness 快速診斷（2026-07-04，Fable 5 session）

> 對象：這個 repo + 這套 Claude Code 環境。每項附弱模型可照做的修法。

## 最漏 token 前三名

**1. 主對話自己下場讀檔、掃 repo。**
主線模型貴（context 累積 + 高 effort），每讀一個大檔都永久佔用 context。
修法：**讀取**超過 2 個檔案的搜尋/閱讀，一律派 Explore agent（`model: haiku`，模糊範圍用 `sonnet`），用 `docs/playbook/prompts.md` 範本 1；主對話只收「結論 + 檔案:行號」。

**2. 進行中計劃無即時狀態，中斷後重推導。**
實例：pipecat 計劃做到 Phase 5，但 Phase 2 完成與否得靠 git status 反推——新 session 要花幾千 token 重建「做到哪了」。
修法：每個進行中的 `tasks/*.md` 頂部維護「## 目前狀態」區塊（≤5 行：做到哪、下一步、阻塞）。完成任一步驟後**立刻**更新，不等收尾。Session 開場先讀這個區塊，不要從頭讀整份計劃。

**3. 整檔重讀與讀不該讀的檔。**
Read 預設抓 2000 行；edit 後 re-read 是純浪費（harness 已保證寫入）。
修法：知道目標區段就用 `offset`/`limit`；edit/write 後禁止 re-read 驗證；`uv.lock`、`pnpm-lock.yaml`、`frontend/.vite/`、GTFS data 檔永不讀。

（非問題：caveman/ponytail hooks 每 session 注入約 250 行——它們省下的輸出 token 遠多於此，**不要「修」它**。island hooks 是外部程序，不吃 context。）

## 最容易失焦前三名

**1. Working tree 累積 30 個 dirty 檔、混多個關注點。**
弱模型分不清哪些改動屬於哪個任務，審查與 commit 都會夾帶。
修法：每完成一個 phase 就 commit（Conventional Commits）；開新任務前 `git status` 必須乾淨或已知。

**2. tasks/*.md 是設計文件、不是狀態板。**
模型讀完 200 行設計，仍不知現在該做哪一步，於是憑感覺挑——常挑錯。
修法：同「目前狀態」區塊；設計內文用 `✅ 已完成` 標記已完成段落（pipecat 計劃 Phase 3/4 已有此標記，照做）。

**3. 同一事實多處記載。**
CLAUDE.md 與 docs/architecture.md 各有一份模組說明，改一處忘另一處後，兩份文件開始互相矛盾，模型會信錯的那份。
修法：單一真相源——模組職責細節只在 `docs/architecture.md`；CLAUDE.md 只放一行指標。發現矛盾時停下回報，不要自行挑一份信。

## 最容易出錯前三名

**1. TDX 方向編碼 0/1（舊 ebus 是 1/2）。**
修法：動到任何含 `direction`、`go_back` 的程式碼，改完必跑 `uv run pytest tests/providers tests/services`；審查用 `prompts.md` 範本 5 的必查清單。

**2. messages 截斷破壞 `tool_call_id` 配對（session.py）。**
修法：動 `backend/agent/session.py` 前，先重讀 CLAUDE.md gotchas 該兩條；改完跑 `uv run pytest tests/agent`。這檔案的截斷邏輯錯一次就會在 runtime 炸 API 400，測試綠才算完成。

**3. Build 產物 / cache 誤入 git（現行犯：`frontend/.vite/deps/` 已 staged）。**
修法：已把 `.vite/` 加進 `frontend/.gitignore` 並 unstage（2026-07-04）。通則：commit 前跑 `git status`，出現非手寫的新檔（cache、build、lock 以外的產物）→ 先查 `.gitignore` 再 commit。
