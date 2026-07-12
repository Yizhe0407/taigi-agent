# PiP 語音對話 UX 重整

## 目前狀態

- **完成**（2026-07-12）：`2941c91`（backend 事件＋注入式 end_conversation tool）、`54cad4f`（前端狀態機＋結束流程）、`a83c22b`（審查 4 findings 修復）。
- 尚未實機驗證：需開 kiosk 實測六態視覺、道別→確認卡時序、45s/15s idle 流程。
- Backlog：前端無測試基礎設施（vitest 未裝），useConversationState 轉換表值得補測——另開任務。

## 問題（研究佐證：scratchpad voice-ui-research.md、pip-flow-map.md）

1. 橘色呼吸框只綁 WebRTC 連線狀態（`PipAgentOverlay.vue:264-268`），connected 就永遠閃，與對話階段脫鉤。
2. 使用者說話中、ASR 辨識中兩階段無任何 UI 回饋——根因是 backend 沒把 VAD 事件送上 data channel（`pipeline.py` 只送 bot_speaking/bot_silent/transcript/agent_reply/subtitle/agent_cancelled）。
3. 結束流程只有右上角 X 與 60s idle timer，無倒數警示、無道別意圖偵測；A→B 使用者輪替無明確流程。
4. `VoiceState` 的 `transcribing` 被挪用來表示「連線中」，語意錯位；60s idle / 30s thinking fuse / 4s TTS watchdog 三組 timer 疊在 overlay。

## 設計定案

### 狀態機（仿 LiveKit AgentState，事件驅動）

前端單一 `ConversationState`，取代舊 `VoiceState`（舊型別與衍生邏輯全刪）：

| 狀態 | 進入事件 | 離開事件 | 視覺 |
|---|---|---|---|
| `connecting` | 開啟 PiP | ICE connected | 中性靜態框＋「連線中…」 |
| `listening` | connected / bot_silent / 各流程收尾 | — | 細靜態 accent 框，**不呼吸** |
| `userSpeaking` | `user_speaking`(VAD) | `user_silent` | 暖色律動框＋「我咧聽…」 |
| `processing` | `user_silent` | `transcript` | 脈動＋「辨識中…」 |
| `thinking` | `transcript` | `bot_speaking`/`subtitle` | 無方向脈動＋思考點點（沿用） |
| `speaking` | `bot_speaking` | `bot_silent` | 穩定色框＋Live2D 嘴型（沿用） |

三個活動態（userSpeaking / processing+thinking / speaking）顏色動畫一眼可辨。每態附狀態 chip 文字。

### Backend 事件補齊（治本）

- `pipeline.py` 轉發 VAD frame 至 data channel：`{"type":"user_speaking"}` / `{"type":"user_silent"}`（VADUserStartedSpeakingFrame / VADUserStoppedSpeakingFrame 已流經 BargeInProcessor 一帶）。

### end_conversation tool（仿 LiveKit EndCallTool）

- Prompt 指示：偵測道別/完成意圖 → **先口語道別** → 呼叫 `end_conversation`。
- 訊號路徑：tool handler 拿不到 session_id（`tool_dispatch.py:86` `handler(**tool_args)`），採**入口注入**（同 `input_enricher` 哲學）：`respond_in_session_stream` 接受可選 extra tool (schema+handler)，voice 的 `agent_processor` 注入 handler closure，內部經 `send_event` 發 `{"type":"end_conversation"}`、回傳 str。REST 路徑不注入、行為不變。`session.py` 不加任何分支。

### 結束/輪替流程（前端）

1. 收 `end_conversation` 事件 → 等 `bot_silent`（道別語播完）→ 顯示確認卡「對話結束囉，猶有問題無？」＋「結束」/「閣繼續問」＋10s 倒數自動結束。
2. Idle timer 重設計：45s 無活動 → 「還在嗎？」倒數 15s 警示卡（任何活動取消）→ 歸零才關閉。取代盲關。
3. 常駐可見「結束對話」按鈕（labeled，非只有小 X）→ B 使用者可立即搶回控制權。
4. 結束 = 關 PiP ＋ **確保 DELETE /api/chat/sessions/{id}**（防 A 的對話脈絡漏給 B）。

## Phase 拆分

- **Phase 1（backend, opus）**：VAD 事件轉發、end_conversation tool＋入口注入、prompt 更新、pytest。commit。
- **Phase 2（frontend, sonnet）**：ConversationState 狀態機、視覺分態、結束/輪替流程、timer 收斂、刪舊邏輯、typecheck。commit。
- **Phase 3（review, sonnet fresh context）**：對照 CLAUDE.md gotchas 審查兩個 diff＋實測。
