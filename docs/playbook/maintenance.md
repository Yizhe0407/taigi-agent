# 誰能改什麼

原則：**事實檔可以直接改，制度檔要先問使用者。** 弱模型改制度，就是制度退化的主要途徑。

| 檔案 | 能自己改嗎 |
|---|---|
| `docs/` 內的事實文件（architecture、changelog…） | 能，程式已改、文件跟上事實時 |
| `docs/playbook/lessons.md` | 能，踩雷後照下方格式追加 |
| `CLAUDE.md` 的「重要 Gotchas」 | 只能**追加**，且要有測試或實錯證據，一次一條 |
| `docs/playbook/` 其他檔、CLAUDE.md 的結構／固定約束／開場協議 | **先問** |
| `~/.claude/` 與 `.claude/settings*.json` | **先問**，使用者私人設定 |
| `.gitignore`、依賴區（`pyproject.toml`、`package.json`） | **先問**，影響面大 |
| 刪除任何 playbook 檔 | **先問**，不可逆 |

分不清就問。

## 踩雷後怎麼記

只記「repo 和文件查不到、又會再犯」的。已在 gotchas 或 git 歷史的不要重複。寫進 `lessons.md`，一則 ≤6 行：

```markdown
## YYYY-MM-DD 一句話標題
- 症狀：看到什麼錯
- 根因：真正原因（不是表象）
- 規則：下次照做的一句話（可執行、有判準）
- 證據：檔案:行號、測試名或 commit
```

## 什麼時候整理

- `lessons.md` 超過 15 則或 100 行，或同主題出現 3 則：把重複的蒸餾成一條 gotcha 加進 CLAUDE.md，原教訓刪除並在 gotcha 尾註 `(源自 lessons YYYY-MM-DD)`。
- 每季：檢查 playbook 的路徑、指令、模型名有沒有過時。過時的列清單問使用者，不要擅改。
- CLAUDE.md ≤150 行。逼近時先刪再加，新內容放 playbook，CLAUDE.md 只留指標。
