# 模型調度守則

> 讀者：主對話裡的任何模型。目的：讓便宜模型組合出高品質結果。
> 事實查證日：2026-07-04（來源：code.claude.com/docs、platform.claude.com/docs）。價格會變，引用前可重查。

## 0. 指揮官不下場

主對話 = 指揮官。指揮官只做三件事：拆任務、下判斷、整合結論。

- 大量讀檔、掃 repo、查網頁、批次改檔、驗證 → 全部派 subagent（Agent tool）。
- 指揮官親自動手的唯一正當理由：**編輯**上限 ≤2 檔且已知位置的小改，派工開銷比做還貴。（**讀取**上限另計，見 `diagnosis.md` 漏 token 第 1 名。）
- 主對話 context 是最貴的資源：subagent 的原始輸出不進主線，只收結論。

## 1. 調度表

| 任務 | agent type | model | 單價(in/out per MTok) |
|---|---|---|---|
| 搜尋、盤點、read-back 驗證 | Explore / general-purpose | `haiku` | $1 / $5 |
| 一般實作、重構、研究、審查 | general-purpose | `sonnet` | $3 / $15 |
| 架構決策、難 debug、高風險審查、第二意見 | general-purpose / Plan | `opus` | $5 / $25 |
| 規劃實作策略 | Plan | `sonnet`（難題 `opus`） | — |
| Claude Code / API 本身的問題 | claude-code-guide | 預設 | — |

模型 ID（需要寫進程式碼時）：`claude-opus-4-8`、`claude-sonnet-4-6`、`claude-haiku-4-5-20251001`。
`fable`（Fable 5）：僅限使用者手動啟用的特殊 session，制度不得依賴它。

### effort 參數

- 本環境的 Agent tool 參數**只有 `model`，沒有 effort**（實測 schema，2026-07-04）。
- effort 可設的地方：主線用 `/effort`（等級 `low|medium|high|xhigh|max`；Opus 4.8 有 `xhigh`，Opus 4.6 / Sonnet 4.6 無）；custom agent 的 frontmatter `effort:`（`.claude/agents/*.md`）。
- 使用者主線預設：`claude-fable-5[1m]` + effort high（見 `~/.claude/settings.json`）。Fable 不在時會是其他模型，勿假設。

## 2. 任務交辦三要素

每次派工的 prompt 必含（範本見 `prompts.md`）：

1. **目標與動機** — 做什麼＋為什麼，讓 agent 能自行判斷邊界情況。
2. **驗收條件** — 可機械核對的清單（測試綠、檔案存在、每條附行號…）。
3. **回報格式** — 格式＋行數上限。

缺任何一項 = 不合格的派工，agent 會自由發揮然後浪費一輪。

## 3. 回報合約

Subagent 回報只准含：

- 結論（bullet，有行數上限）
- `檔案:行號` 引用
- 長產物（>30 行）：先存檔，回傳路徑

禁止：貼整段檔案內容、貼完整 diff、複述任務描述。
指揮官收到違約回報 → 摘要後丟棄，下次派工把上限寫更死。

## 4. 升降級路徑

- **haiku 錯一次** → 同一子任務升 sonnet，prompt 附上 haiku 做了什麼、錯在哪。唯一例外：錯誤訊息已明確指出修法（筆誤、`ModuleNotFoundError`、型別錯誤）→ 同級重試一次，再錯即升。
- **sonnet 同一子任務累計兩次失敗嘗試**（初次失敗＋修一次仍敗；「一次嘗試」= 改動＋驗證一輪）→ 帶完整失敗軌跡（每次改了什麼、每次的錯誤輸出全文）升 opus。不帶軌跡的升級 = 讓 opus 重犯一樣的錯。
- **opus 也解不了** → 停，回報使用者（見 `judgment.md` 第 3 節）。
- **解出模式後降級**：opus/sonnet 解出第一個案例後，把「解法模式 + 一個完整範例」寫成 prompt，降回 haiku/sonnet 批次套用剩餘案例。
- **同一件事最多重試兩輪**。第三輪必須換方法或升級（判準見 `judgment.md` 第 4 節）。

## 5. 驗證不自驗

寫的人不驗自己。每類產出的驗證方式：

- **檔案/文件** → 派 fresh-context agent read-back（`prompts.md` 範本 6，haiku）。
- **程式** → 測試或實跑（`uv run pytest` / `pnpm typecheck`），不是「看起來對」。
- **高風險判斷** → 第二意見：派一個沒看過推理過程的 opus 獨立解同題，比對結論；或產 2–3 個候選答案叫評審 agent 擇優。
- 驗證 agent 的 prompt 不得包含「我認為答案是 X」——會污染判斷。

## 6. 額度心法

- Haiku 便宜 15 倍於 opus：能拆成「opus 想一次、haiku 套用 N 次」的任務就這樣拆。
- 派工前先想「這個 agent 會讀多少」：叫 haiku「掃整個 repo」比叫它「掃 backend/services/ 找 X」貴十倍、準度更低。範圍越窄越好——但必須涵蓋目標可能所在的所有目錄；不確定在哪就先派一次 medium 廣度的 Explore 定位，再窄派。
- 背景 agent（`run_in_background`）適合互不依賴的工作，並行省牆鐘時間，不省 token。
