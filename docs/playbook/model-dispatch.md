# 派 subagent 的規矩

目的：讓便宜的模型組出好結果。主對話是指揮官，只做三件事：**拆任務、下判斷、整合結論。**

- 大量讀檔、掃 repo、查網頁、批次改檔、驗證 → 派 subagent，原始輸出不進主線，只收結論。
- 指揮官自己動手的唯一理由：改 ≤2 個已知位置的檔，派工比做還貴。
- 主對話 context 是最貴的資源。

## 誰做什麼

| 任務 | agent | model |
|---|---|---|
| 搜尋、盤點、read-back 驗證 | Explore / general-purpose | `haiku` |
| 實作、重構、研究、審查 | general-purpose | `sonnet` |
| 架構決策、難 debug、高風險審查、第二意見 | general-purpose / Plan | `opus` |
| 規劃 | Plan | `sonnet`（難題 `opus`） |
| Claude Code / API 的問題 | claude-code-guide | 預設 |

Agent tool **只有 `model` 參數，沒有 effort**。要控制 effort，用主線的 `/effort`，或在 `.claude/agents/*.md` 的 frontmatter 寫 `effort:`。價格與模型 ID 會變，需要時查官方文件，不要憑這裡的記憶。

## 派工 prompt 必含三項

缺任何一項 = 不合格，agent 會自由發揮然後浪費一輪。範本見 `prompts.md`。

1. **目標與動機**：做什麼、為什麼，讓 agent 能判斷邊界情況。
2. **驗收條件**：能機械核對的清單（測試綠、檔案存在、每條附行號）。
3. **回報格式**：格式加行數上限。

## 回報只准含

- 結論（有行數上限）、`檔案:行號`
- 超過 30 行的產物：先存檔，回傳路徑

禁止貼整段檔案、完整 diff、複述任務。收到違規回報就摘要後丟棄，下次把上限寫死。

## 失敗了怎麼升級

| 狀況 | 做法 |
|---|---|
| haiku 錯一次 | 升 sonnet，附上它做了什麼、錯在哪。例外：錯誤訊息已指出修法（筆誤、`ModuleNotFoundError`）→ 同級重試一次，再錯才升 |
| sonnet 累計兩次失敗（一次 = 改動＋驗證一輪） | 帶**完整失敗軌跡**（每次改了什麼、錯誤全文）升 opus。不帶軌跡 = 讓 opus 重犯同樣的錯 |
| opus 也解不了 | 停，回報使用者 |
| 解出模式後 | 把「解法模式＋一個完整範例」寫成 prompt，降回 haiku / sonnet 批次套用 |

同一件事最多重試兩輪，第三輪必須換方法或升級（判準見 `judgment.md`）。

## 寫的人不驗自己

| 產出 | 怎麼驗 |
|---|---|
| 文件 | fresh-context agent read-back（`prompts.md` 範本 6，haiku） |
| 程式 | 測試或實跑（`uv run pytest`、`pnpm typecheck`），不是「看起來對」 |
| 高風險判斷 | 派沒看過推理的 opus 獨立解同一題比對，或產 2–3 個候選叫評審擇優 |

驗證 prompt 不得寫「我認為答案是 X」，會污染判斷。

## 省額度

- 能拆成「opus 想一次、haiku 套用 N 次」就這樣拆。
- 範圍越窄越便宜、越準，但要涵蓋目標可能所在的所有目錄。不確定在哪，先派一次 medium 廣度的 Explore 定位。
- 背景 agent 適合互不依賴的工作，並行省時間，不省 token。
