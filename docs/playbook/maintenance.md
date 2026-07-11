# 維護協議

> 讀者：之後每一個 session 的模型。定義誰能改什麼、教訓寫哪裡、何時整理。

## 檔案修改權限

### 可自行修改（不需詢問使用者）

| 檔案 | 條件 |
|---|---|
| `docs/playbook/lessons.md` | 踩雷後照下方格式追加，隨時可寫 |
| `tasks/*.md` 的「## 目前狀態」區塊 | 完成步驟後立即更新 |
| `TASKS.md` | 進度勾選、狀態更新 |
| `docs/architecture.md` 等 docs/ | 程式碼已改、文件跟上事實時 |
| CLAUDE.md 的「重要 Gotchas」 | 只准**追加**經過驗證的新 gotcha（有測試或實錯證據），一次一條 |
| 專案記憶目錄（memory/*.md） | 照 memory 機制規則 |

### 修改前必須詢問使用者

| 檔案 | 原因 |
|---|---|
| `docs/playbook/model-dispatch.md`、`judgment.md`、`prompts.md`、本檔 | 制度本體。弱模型改制度 = 制度退化的主要途徑 |
| CLAUDE.md 的結構、固定約束、開場協議 | 常載檔，改壞影響所有後續 session |
| `~/.claude/CLAUDE.md`、`~/.claude/settings.json`、`.claude/settings*.json` | 使用者私人設定 |
| `.gitignore`、`pyproject.toml` 依賴區、`package.json` 依賴區 | 影響面大 |
| 刪除任何 playbook 檔或 `CLAUDE.md.bak` | 不可逆 |

判準：**制度檔唯讀、事實檔可寫**。CLAUDE.md 一檔兩區：「重要 Gotchas」是事實區——驗證過即可追加，不必問；其餘（結構、固定約束、開場協議、索引）是制度區——改前要問。其他分不清的就問。

## 踩雷教訓：寫回 `docs/playbook/lessons.md`

格式（一則 ≤6 行）：

```markdown
## YYYY-MM-DD 一句話標題
- 症狀：看到什麼錯
- 根因：真正原因（不是表象）
- 規則：下次照做的一句話（可執行、有判準）
- 證據：檔案:行號 或測試名 或 commit
```

只寫「repo/文件查不到、又會再犯」的教訓。已在 CLAUDE.md gotchas 或 git history 的不要重複寫。

## 整理節奏

- **觸發條件**（任一）：`lessons.md` 超過 15 則或 100 行；或同主題出現 3 則。
- **動作**：把重複出現的教訓蒸餾成一條 gotcha，提案加進 CLAUDE.md（追加 gotcha 可自行做），原教訓從 lessons.md 刪除並在該 gotcha 尾註 `(源自 lessons YYYY-MM-DD)`。
- **每季**（或使用者要求時）：檢查 playbook 各檔的路徑、指令、模型名是否過時；過時項列清單問使用者，不擅改制度檔。
- 常載上限稽核：CLAUDE.md ≤150 行；若逼近，先刪再加——新內容進 playbook，CLAUDE.md 只留指標。
