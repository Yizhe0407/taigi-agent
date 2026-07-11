# 判斷力外化：Rubric 與 Checklist

> 讀者：Sonnet / Haiku 等級模型。每條規則附正例、反例。不確定時照字面執行，不要自行放寬。

## 1. 何時升級模型

升級 = 把同一子任務改派給更強模型（haiku → sonnet → opus），帶上失敗軌跡。

**規則**（照 `model-dispatch.md` 的升降級路徑）：

- 觸發升級的訊號（任一即升級）：
  - haiku 錯一次即升級；sonnet 累計兩次失敗嘗試（初次＋修一次）才升級。「一次嘗試」= 改動＋驗證一輪。與 `model-dispatch.md` 第 4 節同一條規則。
  - 需要跨 3 個以上模組推理因果（例：改 `session.py` 的截斷邏輯會不會弄壞 `tool_call_id` 配對）。
  - 涉及不可逆操作的設計決策（DB schema、公開 API 格式、刪除程式碼超過 50 行）。
- 不該升級的訊號：
  - 只是檔案多、行數多但操作重複（批次改 import、批次改字串）→ 用便宜模型分批做。
  - 錯誤訊息已明確指出修法（`ModuleNotFoundError`、型別錯誤）→ 同級重試一次即可。

**正例**：Haiku 改 `iter_scoped_stop_etas` 的過濾條件，測試紅了，修一次還是紅 → 停手，把「改了什麼、測試輸出全文」寫進 prompt，升級 Sonnet。
**反例**：Haiku 改 20 個檔案的 import 路徑，第 3 個檔案打錯字 → 這是筆誤不是能力問題，同級重做該檔案即可，升級是浪費。

## 2. 何時算真的完成

「完成」必須同時滿足，缺一即未完成，回報時不得寫「完成」：

- [ ] 驗收條件逐條核對過（交辦時的驗收條件，一條一條列出結果）。
- [ ] `cd backend && uv run pytest` 綠（動了 backend 時）；`pnpm typecheck` 過（動了 frontend 時）。
- [ ] `uv run ruff check .` 無新增錯誤。
- [ ] 文件判斷做過：使用方式變 → `README.md`；進度變 → `TASKS.md`；架構變 → `docs/architecture.md`。沒變也要說「判斷過，不需更新」。
- [ ] 回報含「做了什麼、為什麼這樣寫、可能的坑」（CLAUDE.md 固定約束）。

**正例**：「新增 `render_departures` 工具。pytest 12 passed。ruff 乾淨。TASKS.md 已勾。坑：TDX `estimate_seconds` 可能為 None，已在 render 時過濾。」
**反例**：「程式碼寫好了，應該可以動。」→ 沒跑測試 = 未完成。「測試大部分過了」→ 有紅測試 = 未完成，要嘛修好要嘛回報阻塞。

## 3. 何時停下來問使用者

**必須停（不問就做 = 違規）**：

- 刪除或覆寫不是你這個 session 建立的檔案，且內容和任務描述對不上。
- 改 `kiosk_config.json` 語意、API response 欄位名、DB schema — 對外契約。
- 需要花錢或對外發布：部署、發 PR 到別人的 repo、呼叫付費 API 超出測試量。
- 兩個合理方案，選錯的代價是重做一天以上（例：WebRTC 傳輸層選型）。
- 修改 `~/.claude/CLAUDE.md`、`.claude/settings*.json`（見 `maintenance.md` 權限表）。

**不該停（問了 = 浪費使用者時間）**：

- 命名、檔案放哪個目錄 — 照既有慣例（見 CLAUDE.md「新增工具流程」）。
- 測試怎麼寫 — 照 `backend/tests/` 既有模式。
- 錯誤處理細節 — 照同模組既有寫法。

**正例**：發現 `tasks/pipecat_webrtc_plan.md` 和實際程式碼方向矛盾 → 停，回報矛盾點，問要以哪邊為準。
**反例**：不確定 helper 函式要放 `services/` 還是 `tools/` → 不要問，查 CLAUDE.md「新增工具流程」第 2、3 條，照分類放。

## 4. 方向錯的訊號（換路，不是重試）

重試 = 同方法再來一次。換路 = 承認方法錯，回到上一個決策點。**同一方法最多兩輪，第三次必須換路或升級。**

方向錯的訊號：

- 修 A 壞 B、修 B 壞 A，來回兩次 → 抽象層級選錯了，停下來畫出依賴關係再動手。
- 為了讓測試過而改測試預期值，卻說不出為什麼新值才對 → 你在湊答案，回到需求重讀。
- diff 越改越大但驗收條件沒有更接近 → 回滾到最後一個綠的狀態（`git stash` / `git checkout -- <file>`），重新規劃。
- 需要 mock 越來越多內部函式才能測 → 邊界切錯了，先重看 `docs/architecture.md` 的分層。
- 同一個錯誤訊息出現第三次 → 你沒有真的理解它，把錯誤全文貼進升級 prompt。

**正例**：改 pipecat pipeline 讓 STT 過了但 TTS 斷流，修 TTS 又弄壞 STT → 第二輪就停，回報「frame 順序假設可能錯了」，帶軌跡升級。
**反例**：測試說 `assert direction == 0` 失敗，就把測試改成 `== 1` — 先查 CLAUDE.md：TDX Direction 0=去程。改測試前必須能引用文件或程式碼證明測試本來就錯。

## 5. 品質底線如何驗證

原則：**寫的人不驗自己**。驗證用 fresh-context agent 或機械手段，不靠「我覺得沒問題」。

| 產出類型 | 最低驗證手段 |
|---|---|
| 程式碼 | `uv run pytest` + `uv run ruff check .`（frontend：`pnpm typecheck`）。無測試覆蓋的新邏輯 → 補一個最小測試再交 |
| 文件 / 設定檔 | 派 fresh-context agent read-back：只給檔案路徑，要它回答「這份文件叫你做什麼」，答不出 = 文件不合格 |
| 高風險判斷（架構、選型） | 第二意見：派一個沒看過你推理的 agent 獨立解同一題，比對結論；不一致 → 升級或問使用者 |
| 批次修改 | 抽 3 個樣本人工檢查 + 全量跑 lint/測試 |

**正例**：重寫 CLAUDE.md 後，派 fresh agent 問「照這份文件，新增一個 provider 要動哪些檔案？」答案對 = 過。
**反例**：「我重讀了一遍自己寫的文件，沒問題」— 寫的人腦中有上下文，讀不出缺漏，這不算驗證。
