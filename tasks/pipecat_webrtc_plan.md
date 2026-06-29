# Pipecat WebRTC 導入計畫 (Small WebRTC Transport)

## 1. 目標 (Objective)

將現有的 REST 語音架構 (HTTP POST ASR -> LLM -> HTTP TTS) 替換為基於 **Pipecat** 的 WebRTC 全雙工串流架構。
旨在解決 Kiosk / 平板場域中的以下痛點：

- **AEC (回音消除)**：利用 WebRTC 喚醒瀏覽器/硬體底層的 AEC，避免 AI 自己打斷自己。
- **Barge-in (打斷機制)**：利用 Pipecat 內建的 Pipeline 中斷機制，實現低延遲的全雙工對話。
- **延遲 (Latency)**：將原本的 Request/Response 轉化為 Chunk-based streaming。

## 2. 架構決策 (Architecture Decisions)

- **傳輸層 (Transport)**：使用 Pipecat 支援的 WebRTC Transport (例如 Daily 或 LiveKit Transport)。
  > *註：若強烈排斥 LiveKit Server 的 Room 概念，最快上手的是 DailyTransport (底層走 Daily.co 的基礎設施，開發者僅需關注 Pipeline)，或者使用 Pipecat 的 Local WebRTC 擴充套件。*
- **後端管線 (Backend Pipeline)**：
  - `VAD` (Silero) -> `ASR` (現有 Qwen3-ASR 改寫為 Streaming/Chunk 接收) -> `LLM/Agent` -> `TTS` (改寫為 Streaming 輸出)。
- **前端 (Frontend)**：移除原有的 `useAudioRecorder.ts` 和 `useTts.ts`，改用對應的 WebRTC SDK (例如 `daily-js` 或純 WebRTC API) 建立 PeerConnection。

## 3. 實作步驟 (Implementation Steps)

### Phase 1: 基礎環境與 PoC (Proof of Concept)

1. **安裝依賴**：在 `backend` 安裝 `pipecat-ai` 及相關 Transport 模組。

   ```bash
   uv add pipecat-ai
   uv add pipecat-ai[daily] # 如果選擇 Daily 作為 WebRTC Provider
   ```

2. **建立 Echo Bot**：寫一個最簡單的 Pipecat script，只包含 Transport 與 VAD，收到前端聲音後直接回聲，確認 WebRTC 連線與 AEC 在平板上正常運作。

### Phase 2: 後端模組封裝 (Custom Services)

現有專案是 Qwen3-ASR 與自建 Agent，必須將它們封裝成符合 Pipecat `Service` 介面的類別：

1. **ASR Service (`Qwen3ASRService`)**：
   - 繼承 `pipecat.services.ai_services.STTService`。
   - 實作接收 PCM chunk 的邏輯，累積到 VAD 觸發 `SpeechEnd` 時，送交 Qwen3 轉文字。
2. **LLM/Agent Service (`AgentService`)**：
   - 繼承 `pipecat.services.ai_services.LLMService`。
   - 將現有的 RAG / Tools 邏輯包裝進來，並確保以非同步 (async generator) 方式產出文字。
3. **TTS Service (`CustomTTSService`)**：
   - 繼承 `pipecat.services.ai_services.TTSService`。
   - 接收文字串流，轉化為 PCM audio frame 往 Pipeline 丟。

### Phase 3: Pipeline 組合與 Barge-in 設定

在後端入口 (FastAPI) 中，當收到前端連線請求時：

1. 建立 `Pipeline`：`[transport.input(), vad, asr, agent, tts, transport.output()]`
2. 建立 `PipelineTask`，並明確啟用打斷機制。
3. 監聽 VAD 事件：當 VAD 觸發 `UserStartedSpeaking` 時，Pipecat 會自動清理後端排隊中的 TTS Frame，達到瞬間閉嘴的效果。

### Phase 4: 前端重構

1. **移除舊有 REST API**：移除 `api/chat.ts` 中的 `transcribeAudio` 與 `synthesizeSpeech`。
2. **整合 WebRTC Client**：
   - 前端發起請求給 FastAPI 獲取 WebRTC Token/URL。
   - 呼叫 `getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } })`。
   - 將 MediaStream 綁定到 WebRTC Client 並與後端連線。
   - （可選）保留前端的 Silero VAD 用於 UI 動畫 (嘴型/聲波) 與本機偵測，但不負擔核心的錄音切塊與傳輸。

## 4. 預期風險與解法

- **Qwen3-ASR Streaming 問題**：Qwen3 可能不支援真正的流式辨識 (Streaming ASR)。
  - **解法**：Pipecat 支援 Endpointing。我們可以讓 Pipecat 後端 VAD 累積一段完整的 PCM Buffer，等使用者講完 (Speech End) 再一次丟給 Qwen3-ASR。這在 Pipecat 架構中依然是支援的，並且不會影響 TTS 的串流與打斷。
- **基礎設施依賴**：如果 Kiosk 場域無法連外網，只能在內部區網，則無法使用 Daily.co。
  - **解法**：只能退回架設簡易版 LiveKit Server (單節點)，或者考慮使用 Pipecat 的 `FastAPIWebsocketTransport`（犧牲硬體 AEC 換取純地端）。

## 5. 驗收標準 (Acceptance Criteria)

1. 在平板/筆電上開啟網頁，不需點擊即可連續對話。
2. AI 講話時，使用者可直接講話打斷，AI 會在 200ms 內停止發聲。
3. 測試環境中，AI 的聲音不會引發系統無限迴圈 (AEC 成功生效)。
