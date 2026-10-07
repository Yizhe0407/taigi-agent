# 什麼時候升級、完成、發問、換路

不確定時照字面執行，不要自行放寬。

## 升級模型

升級升什麼、怎麼升，見 `model-dispatch.md`。這裡只講**要不要升**：

| 該升 | 不該升 |
|---|---|
| 跨 3 個以上模組推理因果（改 `session.py` 的截斷會不會弄壞 `tool_call_id` 配對） | 檔案多但操作重複（批次改 import、改字串）→ 便宜模型分批做 |
| 不可逆的設計決策（DB schema、公開 API、刪超過 50 行） | 錯誤訊息已指出修法 → 同級重試一次 |

例：Haiku 改 `iter_scoped_stop_etas` 的過濾，測試紅，修一次還紅 → 停，把改動和測試輸出全文寫進 prompt，升 Sonnet。
反例：Haiku 改 20 個檔的 import，第 3 個打錯字 → 是筆誤，同級重做那個檔，升級是浪費。

## 什麼才算完成

以下**全部**成立才能說「完成」，缺一就不是：

- [ ] 驗收條件逐條核對過
- [ ] 動了 backend：`cd backend && uv run pytest` 綠；動了 frontend：`pnpm typecheck` 過
- [ ] `uv run ruff check .` 沒有新增錯誤
- [ ] 文件判斷做過：用法變 → `README.md`；功能或限制變 → `docs/changelog.md`；架構變 → `docs/architecture.md`。沒變也要說「判斷過，不需更新」
- [ ] 回報含「做了什麼、為什麼、可能的坑」

好的回報：「新增 `render_departures`。pytest 12 passed，ruff 乾淨，changelog 已更新。坑：TDX `estimate_seconds` 可能是 None，render 時已過濾。」
壞的回報：「寫好了，應該可以動。」（沒跑測試）「測試大部分過了。」（有紅的就是未完成，要嘛修好要嘛回報阻塞）

## 什麼時候停下來問使用者

**必須問**，不問就做是違規：

- 刪除或覆寫不是你這個 session 建的檔案，且內容和任務描述對不上
- 改 `kiosk_config.json` 語意、API 欄位名、DB schema（對外契約）
- 要花錢或對外發布：部署、發 PR 到別人的 repo、超出測試量的付費 API
- 兩個合理方案，選錯要重做一天以上（例：WebRTC 傳輸層選型）
- 改 `~/.claude/CLAUDE.md`、`.claude/settings*.json`

**不要問**，問了是浪費時間：

- 命名、檔案放哪 → 照 CLAUDE.md「新增工具流程」
- 測試怎麼寫 → 照 `backend/tests/` 既有模式
- 錯誤處理細節 → 照同模組既有寫法

## 方向錯的訊號

重試是同方法再來一次，換路是承認方法錯、回到上個決策點。**同一方法最多兩輪。**

- 修 A 壞 B、修 B 壞 A，來回兩次 → 抽象層級選錯，停下來畫依賴再動手
- 為了讓測試過而改測試預期值，卻說不出新值為何才對 → 你在湊答案，回到需求重讀
- diff 越改越大，離驗收條件沒更近 → 回滾到最後一個綠的狀態重新規劃
- 要 mock 越來越多內部函式才能測 → 邊界切錯，重看 `docs/architecture-reference.md` 的分層
- 同一個錯誤第三次出現 → 你沒真的理解它，把錯誤全文放進升級 prompt

例：改 pipecat pipeline，STT 過了但 TTS 斷流，修 TTS 又弄壞 STT → 第二輪就停，回報「frame 順序假設可能錯了」並升級。
反例：測試 `assert direction == 0` 失敗就改成 `== 1`。先查 CLAUDE.md：TDX 0=去程。改測試前要能引用文件或程式證明測試本來就錯。

## 怎麼驗品質

寫的人不驗自己，用 fresh-context agent 或機械手段：

| 產出 | 最低驗證 |
|---|---|
| 程式 | `uv run pytest` + `uv run ruff check .`（frontend：`pnpm typecheck`）；沒測試的新邏輯補一個最小測試 |
| 文件 | 派 fresh agent，只給檔案路徑，問「照這份做會怎樣」或幾個理解題，答不出就是文件不合格 |
| 高風險判斷 | 派沒看過推理的 agent 獨立解同題，結論不一致就升級或問使用者 |
| 批次修改 | 抽 3 個人工檢查，加全量 lint 與測試 |

「我重讀了一遍沒問題」不算驗證：寫的人腦中有上下文，讀不出缺漏。
