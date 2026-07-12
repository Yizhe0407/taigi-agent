import { readonly, ref } from "vue"

import type { ConversationState } from "../types"

/**
 * Single source of truth for the PiP voice conversation phase (仿 LiveKit
 * AgentState). Every transition is driven by exactly one backend event —
 * no ad hoc booleans (`isWebRTCThinking` etc.) shadow this state elsewhere.
 *
 * Table (see tasks/pip-voice-ux.md):
 *   connecting -> listening -> userSpeaking -> processing -> thinking -> speaking -> listening
 *
 * Defensive transitions:
 *   - `user_speaking` is accepted from ANY state (VAD is ground truth for
 *     "who's talking now"), including `speaking` — that's barge-in.
 *   - `agent_cancelled` resolves back to `userSpeaking` or `listening`
 *     depending on the last VAD signal, since a cancelled turn can land at
 *     either depending on whether the user kept talking through the barge-in.
 */
export function useConversationState() {
  const state = ref<ConversationState>("connecting")
  // Last VAD signal — decides where onAgentCancelled() lands.
  let userIsSpeaking = false

  function reset() {
    state.value = "connecting"
    userIsSpeaking = false
  }

  function setConnecting() {
    state.value = "connecting"
  }

  function setListening() {
    state.value = "listening"
  }

  function onUserSpeaking() {
    userIsSpeaking = true
    state.value = "userSpeaking"
  }

  function onUserSilent() {
    userIsSpeaking = false
    if (state.value === "userSpeaking") state.value = "processing"
  }

  function onTranscript() {
    // Normally arrives from `processing`; accepted defensively from any
    // state in case a `user_silent` event was dropped.
    state.value = "thinking"
  }

  function onBotSpeaking() {
    state.value = "speaking"
  }

  function onSubtitle() {
    // Some short replies fire `subtitle` without a preceding `bot_speaking` —
    // treat it as an equivalent leave-`thinking` signal.
    if (state.value === "thinking") state.value = "speaking"
  }

  function onBotSilent() {
    if (state.value === "speaking") state.value = "listening"
  }

  function onAgentCancelled() {
    state.value = userIsSpeaking ? "userSpeaking" : "listening"
  }

  /** Fallback for stuck timers (thinking/processing fuse, TTS watchdog): the
   * pipeline never followed up, so hand control back to the user instead of
   * leaving the UI frozen mid-phase. */
  function forceListening() {
    state.value = "listening"
  }

  /** WebRTC dropped (ICE failed/closed while the PiP is still open) — freeze
   * into a static error visual; data-channel events can no longer arrive so
   * no other transition will ever fire. Exit is the close/end button. */
  function setError() {
    state.value = "error"
    userIsSpeaking = false
  }

  return {
    state: readonly(state),
    reset,
    setConnecting,
    setListening,
    onUserSpeaking,
    onUserSilent,
    onTranscript,
    onBotSpeaking,
    onSubtitle,
    onBotSilent,
    onAgentCancelled,
    forceListening,
    setError,
  }
}
