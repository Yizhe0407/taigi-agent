# Pipecat WebRTC 導入計畫

## 目前狀態（2026-07-07）

- Phase 1–4 完成並 commit（ce027de）。
- 2026-07-07 深度審查發現 13 項問題（見下方「審查發現」）→ 四路並行修復完成：#1-#13 全修（#9 改為 bot 說話時才 interrupt；#12 pre-commit 順手修）。fresh-context 複審 2 findings（renegotiation 孤兒 session、假測試）也已修。pytest 250 綠、ruff 乾淨、typecheck 綠。
- Phase 5：telemetry 整合完成——session connect/disconnect + active_sessions、barge-in counter、語音輪延遲 histogram、`voice.tts` stage 耗時、pipeline 異常路徑 `log_diagnostic`，見 `docs/observability.md`。
- 下一步：Phase 5 — 平板 AEC 實測、barge-in 延遲 <200ms 驗證、斷線重連 fallback（含 ICE failed 時 pcId 未清的重連路徑）、telemetry 整合、architecture.md voice 段落更新。
- 阻塞：無。

## 審查發現（2026-07-07，已對照 pipecat 1.4.0 原始碼驗證）

高優先：

1. **Pipeline 永不終止**：`voice/pipeline.py` on_client_disconnected 只 log，pipecat 的 `_on_client_disconnected` 不會推 EndFrame → 每次開關 overlay 洩漏一整條 pipeline（Silero ONNX、tasks）。目前靠 PipelineParams 預設 `idle_timeout_secs=300` + `cancel_on_idle_timeout=True` 五分鐘後自殺兜底。修法：disconnect handler 裡 `await task.cancel()`。
2. **Session race**：PipAgentOverlay open 時 `ensureSession()`（REST POST）與 `webrtcConnect()` 併發；offer 送出時 sessionId 可能還是 null → 後端建 fallback session → 語音與文字各聊各的。修法：connect 前 await session 建立。
3. **STT 吃掉語音開頭**：BreezeSTTService 只在收到 VADUserStartedSpeakingFrame 之後才開始 buffer，Silero 確認延遲 ~200-400ms 的音訊永久遺失。pipecat 內建 `SegmentedSTTService` 就是做這件事且有 1 秒 pre-speech rolling buffer——改繼承它、只實作 `run_stt()`，可刪 ~60 行。
4. **Idle 自殺傷及活連線**：同第 1 點的 300s idle cancel——使用者靜默 5 分鐘（overlay 開著）pipeline 自殺，WebRTC 仍顯示 connected、麥克風死。修 1 之後把 `idle_timeout_secs=None` 或處理 on_idle_timeout 通知前端。
5. **前端 thinking 卡死**：agent_processor 只在成功時 `_send_event("agent_reply")`；inference 被 barge-in cancel、LookupError、或 ASR 空結果時前端 `isWebRTCThinking` 永遠 true 且 idleTimeout 已被 clear → overlay 永不自動關。錯誤 fallback 有推 TTS 但沒推 data-channel event，chat 面板也看不到。

中優先：

6. **同 session 併發寫入 lost update**：文字 POST 與語音 transcription 同時跑 `respond_in_session`（load→respond→save），後存者覆蓋前者訊息。修法：process-wide per-session asyncio.Lock。
7. **`_chunk_queue.clear()` 留下未 resolve 的 future**：aiortc RawAudioTrack queue 是 `(chunk, future)`，`add_audio_bytes` 的 await 靠 popleft resolve。現在安全只因預設路徑 MediaSender.handle_interruptions 會 cancel audio task；若未來用 mixer/uninterruptible frames 就全音訊死鎖。改成 popleft 迴圈並 `future.set_result(True)`。
8. **PipelineTask/PipelineParams 已 deprecated**（1.3 起，2.0 移除）：改用 `PipelineWorker` + `pipecat.workers.base_worker.WorkerParams`。
9. **雜訊觸發 barge-in 會取消 inference**：咳嗽/環境音 >start_secs → 取消進行中的 agent 推理；若後續 ASR 回空，原問題無聲丟失（與第 5 點複合成卡死）。考慮只在 bot 說話時 interrupt。

低優先：

10. Welcome 用裸 TextFrame 走 sentence aggregator（靠「？」flush）——canonical 是 `TTSSpeakFrame`。
11. `BreezeSTTService` 沒關 `audio_passthrough`（預設 True）→ 音訊 frame 一路流過 agent/TTS 白跑。改 SegmentedSTTService 時一併處理。
12. ruff 1 error：pipeline.py `asyncio.TimeoutError` → builtin `TimeoutError`（`--fix` 可修）。
13. `backend/voice/` 零測試；`_start_pipeline` 內部錯誤被 SmallWebRTCRequestHandler 吞掉（log 後仍回 SDP answer）→ 前端連上死 pipeline。

## 1. 目標 (Objective)

將現有的 REST 語音架構 (HTTP POST ASR → Chat → HTTP TTS) 替換為基於 **Pipecat** 的 WebRTC 全雙工串流架構。
旨在解決 Kiosk / 平板場域中的以下痛點：

- **AEC (回音消除)**：利用 WebRTC 喚醒瀏覽器/硬體底層的 AEC，避免 AI 自己打斷自己。
- **Barge-in (打斷機制)**：利用 Pipecat 內建的 Pipeline 中斷機制，實現低延遲的全雙工對話。
- **延遲 (Latency)**：將原本的三段式 Request/Response 轉化為單一 WebRTC 連線上的串流處理。

### 現有架構（將被取代）

```text
前端按鈕錄音 → blobToWav → POST /api/asr → 文字
                                                ↓
                              POST /api/chat/sessions/{id}/messages → 回覆文字
                                                                        ↓
                                                    POST /api/tts → WAV → HTMLAudioElement 播放
```

### 目標架構

```text
瀏覽器 getUserMedia ←──WebRTC PeerConnection──→ Pipecat Pipeline
        (AEC 由瀏覽器硬體層處理)                    │
                                                   ├─ transport.input()
                                                   ├─ Silero VAD
                                                   ├─ BreezeSTTService (breeze-asr-26)
                                                   ├─ TaigiBusAgentProcessor (現有 AgentSession)
                                                   ├─ TaigiTTSService (HanloFlow → Taibun → Piper)
                                                   └─ transport.output()
```

## 2. 架構決策 (Architecture Decisions)

### 2.1 傳輸層：SmallWebRTCTransport

**決定使用 Pipecat 內建的 `SmallWebRTCTransport`**，不用 Daily 或 LiveKit。

理由：

- Kiosk 是固定單台設備，不需 Room / SFU 概念
- 不依賴外網（雲林站牌場域可能網路不穩或只有內網）
- 零外部基礎設施，部署就是一個 Python process
- 直接跟 FastAPI 整合，用 HTTP POST 交換 SDP offer/answer

### 2.2 Agent 整合：自定義 FrameProcessor

**不繼承 `LLMService`**。現有 `AgentSession` 包含 `IntentRouter`（regex 意圖分類）、`ConvState`、tool-call loop、context cap 等複雜邏輯，無法映射到 Pipecat 的 `LLMService` 介面。

做法：寫一個 `FrameProcessor`，接收 `TranscriptionFrame`（ASR 輸出的文字），呼叫 `AgentSession.respond(text)`，推出 `TextFrame`（給 TTS）。

```python
class TaigiBusAgentProcessor(FrameProcessor):
    """Wrap existing AgentSession as a Pipecat FrameProcessor."""
    async def process_frame(self, frame, direction):
        if isinstance(frame, TranscriptionFrame):
            reply = await self._session.respond(frame.text)
            await self.push_frame(TextFrame(text=reply))
        else:
            await self.push_frame(frame, direction)
```

這樣**完全不改現有 Agent/tool/prompt 邏輯**。

### 2.3 ASR：breeze-asr-26（非 Streaming）

ASR 使用 **breeze-asr-26**（MediaTek Research，基於 Whisper large-v2 微調，專精台語 + 國台英 code-switching）。

- breeze-asr-26 不支援 streaming ASR → 使用 Pipecat VAD endpointing：累積完整語句的 PCM buffer，`SpeechEnd` 時一次送出辨識
- 後端仍透過 HTTP proxy 送到 ASR 服務（與現有 `api/asr.py` 邏輯一致）
- ASR 模型可透過 `ASR_BASE_URL` / `ASR_MODEL` env var 隨時切換（例如未來換 Qwen3-ASR-0.6B 微調版）

### 2.4 TTS：保留完整台語轉寫 Pipeline

TTS 不是直接送文字給語音合成 API，中間有一段完整的台語轉寫 pipeline：

```text
華語文字 → normalize_for_tts() → HanloFlow(漢羅) → Taibun(台羅拼音) → 標點切段 → 並行 Piper TTS → PCM frames
```

Custom TTS Service 必須完整保留 `pipeline/tts_normalizer.py` + `pipeline/text_processor.py` 的邏輯，輸出 PCM `AudioRawFrame`（非 WAV，WebRTC transport 直接吃 PCM）。

### 2.5 前端保留 REST Fallback

- 現有 REST endpoints（`/api/asr`、`/api/tts`、`/api/chat`）完整保留
- WebRTC 語音作為可選功能，前端以 feature flag 或 UI toggle 切換
- 文字聊天面板照舊走 REST
- 好處：WebRTC 開發期間不影響現有功能和 demo

## 3. 實作步驟 (Implementation Steps)

### Phase 2: Custom Services 封裝（~2-3 天）

新增目錄結構：

```text
backend/voice/
  __init__.py
  stt_breeze.py        # BreezeSTTService
  tts_taigi.py         # TaigiTTSService
  agent_processor.py   # TaigiBusAgentProcessor
  pipeline.py          # Pipeline 組裝 + lifecycle
```

#### 2a. BreezeSTTService

- 繼承 `pipecat.services.stt.STTService`
- 實作 `run_stt(audio: bytes) -> AsyncGenerator[Frame, None]`
- Pipecat 的 `STTService` base class 處理 VAD → buffer accumulation → 呼叫 `run_stt()`
- 內部邏輯複用 `api/asr.py` 的 HTTP proxy：把 PCM buffer 轉 WAV，POST 到 `ASR_BASE_URL/v1/audio/transcriptions`
- 加上 telemetry span（`stt.breeze`）

#### 2b. TaigiTTSService

- 繼承 `pipecat.services.tts.TTSService`
- 實作 `run_tts(text: str) -> AsyncGenerator[Frame, None]`
- 完整保留現有 pipeline：
  1. `normalize_for_tts(text)` — 數字/時間/路線號正規化
  2. `text_process_async(text)` — HanloFlow → Taibun
  3. `_split_tailo(tailo)` — 標點切段
  4. 各段並行送 Piper TTS（`/v1/audio/speech`）
  5. 輸出 PCM `AudioRawFrame`（非 WAV），段間插靜音 frame
- 加上 telemetry span（`tts.taigi`）

#### 2c. TaigiBusAgentProcessor

- 自定義 `FrameProcessor`（不繼承 `LLMService`）
- 接收 `TranscriptionFrame` → `AgentSession.respond(text)` → 推出 `TextFrame`
- 同時透過 data channel 推送事件給前端：

  ```json
  {"type": "transcript", "text": "我要去斗六", "role": "user"}
  {"type": "agent_reply", "text": "好的，我來查看斗六方向的公車...", "role": "assistant"}
  ```

- Session 生命週期由前端控制：前端透過 `session_id` 建立或重用 session，讓 WebRTC 與 REST 共用相同的 context
- 使用 `ChatSessionStore` 持久化 messages（跟 REST 路徑一致）

### Phase 3: Pipeline 組裝 + Barge-in（✅ 已完成）

```python
pipeline = Pipeline([
    transport.input(),        # WebRTC audio in
    silero_vad,               # Silero VAD（endpointing）
    breeze_stt,               # breeze-asr-26（buffer → transcribe）
    agent_processor,          # AgentSession.respond()
    taigi_tts,                # HanloFlow → Taibun → Piper TTS
    transport.output(),       # WebRTC audio out
], allow_interruptions=True)  # ← 啟用 Barge-in
```

- `allow_interruptions=True`：VAD 偵測到使用者說話時，Pipecat 自動清理 TTS queue 中排隊的 audio frames，AI 瞬間閉嘴
- 監聽 `on_client_connected`：透過 data channel 等待 `client_ready` 握手後送歡迎語（「請問您欲前往哪裡？」）
- 監聽 `on_client_disconnected`：不再自動清理 Session，保留 Context 給重連使用
- 每個瀏覽器連線 → 獨立的 `PipelineTask`（不同使用者不共用）
- 加入 pipeline error handler：ASR/TTS 服務異常時的 graceful degradation

### Phase 4: 前端整合（✅ 已完成）

#### 4a. 新增 `useWebRTC.ts` composable

```text
frontend/src/features/agent-chat/composables/useWebRTC.ts
```

- `connect()`：POST `/api/voice/offer` 交換 SDP，建立 `RTCPeerConnection`
- `disconnect()`：關閉連線
- 從 remote `MediaStream` 接出 `AnalyserNode` → `mouthAmplitude` ref（驅動 Live2D 嘴型動畫）
- 監聽 data channel 接收 `transcript` / `agent_reply` 事件，更新 `messages` 陣列
- 連線狀態管理（`connecting` / `connected` / `disconnected` / `error`）
- 自動 reconnection（平板瀏覽器閃退後恢復）

#### 4b. 修改 PipFrame.vue / PipAgentOverlay.vue

- 新增「開啟/關閉語音」toggle 按鈕（取代現有的「按住說話」）
- 語音開啟後持續串流（全雙工），不需按住
- 嘴型動畫改從 WebRTC remote stream 的 `AnalyserNode` 取 amplitude
- 字幕動畫改由 data channel 事件驅動（不再依賴 TTS response 的 durationMs）

#### 4c. 保留文字聊天面板

- `PipChatPanel.vue` 的文字輸入照舊走 REST（`/api/chat/sessions/{id}/messages`）
- 語音模式下收到的 transcript / reply 也同步顯示在聊天記錄中
- 兩條路徑共用同一個 `ChatSessionStore` session

#### 4d. 清理舊有 REST 模組（✅ 已完成）

- `useAudioRecorder.ts`、`useVoiceInput.ts` 已被安全移除。
- `api/chat.ts` 的 `transcribeAudio` 已移除。
- WebRTC 模式啟用時，將自動 suppress `useTts.ts` 避免雙重播音，文字聊天仍可共存。

### Phase 5: 收尾 + 測試（進行中）

1. 平板實機測試 AEC（最重要）
2. Barge-in 延遲測量（目標 < 200ms）
3. 網路不穩時的 reconnection / fallback 邏輯
4. Telemetry 整合：voice session 的 span / metrics（複用現有 `telemetry.py`）
5. 更新 `docs/architecture.md` 加入 voice pipeline 段落
6. ✅ 清理已棄用的 REST 語音模組（已於 Phase 4 提早完成）

## 4. 預期風險與解法

| 風險 | 機率 | 影響 | 解法 |
| ------ | ------ | ------ | ------ |
| breeze-asr-26 非 streaming 造成的辨識延遲 | 低 | 中 | VAD endpointing 是最佳實踐，延遲主要在模型端；未來可換 Qwen3-ASR-0.6B 加速 |
| TTS pipeline（HanloFlow + Taibun + Piper）延遲 | 中 | 中 | 分段並行 TTS 已有實作；可考慮 pipeline 化（第一段出來就開始播） |
| Pipecat + FastAPI event loop 共存 | 低 | 中 | Pipecat 官方範例即在 FastAPI 中運行 |
| 平板閃退 / 網路斷線的 session 洩漏 | 中 | 低 | Pipeline 設 idle timeout；`on_client_disconnected` 清理 |
| Live2D 嘴型接 WebRTC stream | 低 | 低 | Web Audio API 接 MediaStream 是標準做法 |

## 5. 驗收標準 (Acceptance Criteria)

1. 在平板/筆電上開啟網頁，點擊「開啟語音」後可連續全雙工對話，不需每次按住錄音。
2. AI 講話時，使用者可直接講話打斷，AI 在 200ms 內停止發聲。
3. 測試環境中，AI 的 TTS 聲音不會被麥克風收進去引發無限迴圈（AEC 生效）。
4. 語音對話的 transcript 與 agent reply 同步顯示在聊天面板中。
5. Live2D 嘴型動畫在 WebRTC 模式下正常驅動。
6. WebRTC 連線失敗或斷開時，可 fallback 到現有 REST 語音模式。

## 6. 工期估算

| Phase | 預估天數 | 依賴 |
| ------- | --------- | ------ |
| Phase 2: Custom Services | 2-3 天 | 無 |
| Phase 3: Pipeline + Barge-in | 1-2 天 | Phase 2 |
| Phase 4: 前端整合 | 2-3 天 | Phase 3 |
| Phase 5: 測試收尾 | 1-2 天 | Phase 4 |
| **合計** | **6-10 天** | |
