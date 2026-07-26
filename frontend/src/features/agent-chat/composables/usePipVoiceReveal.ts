import type { Ref } from "vue"

import type { PipChatMessage } from "../types"

const TTS_WATCHDOG_MS = 4000

/**
 * Voice reply turn model: single owner of the assistant voice bubble. Each
 * user utterance opens a turn (`beginVoiceTurn`); only that turn's events may
 * touch its bubble, so a late/trailing event from a prior turn (classically a
 * barged-in turn's `bot_silent` arriving after the next transcript) can no
 * longer corrupt the new bubble.
 *
 * Two pieces, tangled together because the watchdog's fallback is "flush
 * whatever text is parked":
 *   - char-by-char reveal of each `subtitle` segment over its audio duration
 *     (`revealSegment`/`stopReveal`), guarded by a token bumped on every turn
 *     boundary/interruption so a superseding segment can finish the old one
 *     instantly (`finishRevealTail`).
 *   - the 4s TTS-dead watchdog: fires only if the LLM answered (a reply is
 *     parked) but no audio ever started — applies the parked text so it's
 *     not lost, then hands control back via `onWatchdogExpire`.
 */
export function usePipVoiceReveal(opts: {
  messages: Ref<PipChatMessage[]>
  displayedAgentText: Ref<string>
  /** TTS proven dead: parked reply has just been flushed onto the bubble. */
  onWatchdogExpire: () => void
}) {
  let voiceTurnId = 0
  let replyBubbleId: string | null = null
  let parkedReply: string | null = null
  let audioStarted = false

  let revealToken = 0
  let revealTail: { text: string; index: number } | null = null

  let ttsWatchdog: number | null = null

  function clearTtsWatchdog() {
    if (ttsWatchdog !== null) { clearTimeout(ttsWatchdog); ttsWatchdog = null }
  }

  function ensureReplyBubble(): PipChatMessage {
    const existing = replyBubbleId
      ? opts.messages.value.find(m => m.id === replyBubbleId)
      : undefined
    if (existing) return existing
    replyBubbleId = `voice-reply-${voiceTurnId}`
    const bubble: PipChatMessage = { id: replyBubbleId, role: "assistant", text: "" }
    opts.messages.value.push(bubble)
    opts.displayedAgentText.value = ""
    return bubble
  }

  function stopReveal() {
    // Stop without completing: leave the bubble at whatever was actually
    // spoken (finishing here would show text the user never heard).
    revealToken++
    revealTail = null
  }

  function finishRevealTail() {
    // A newer segment arrived mid-reveal — audio 已前進，直接補完舊段落，字幕才不落後。
    if (!revealTail) return
    const rest = revealTail.text.slice(revealTail.index)
    revealTail = null
    if (!rest || !replyBubbleId) return
    const bubble = opts.messages.value.find(m => m.id === replyBubbleId)
    if (bubble) bubble.text += rest
    opts.displayedAgentText.value += rest
  }

  async function animateReveal(text: string, durationMs: number, token: number) {
    const delay = text.length ? Math.max(15, durationMs / text.length) : 0
    for (let i = 0; i < text.length; i++) {
      if (token !== revealToken) return // superseded by a newer segment, or stopped
      const bubble = replyBubbleId ? opts.messages.value.find(m => m.id === replyBubbleId) : null
      if (bubble) bubble.text += text[i]
      opts.displayedAgentText.value += text[i]
      revealTail = { text, index: i + 1 }
      if (delay > 0) await new Promise<void>(r => setTimeout(r, delay))
    }
    if (token === revealToken) revealTail = null
  }

  function revealSegment(text: string, durationMs: number) {
    finishRevealTail() // finish whatever segment was still revealing
    ensureReplyBubble()
    const token = ++revealToken
    void animateReveal(text, durationMs, token)
  }

  function applyParkedReply() {
    // Overwrite the bubble with the authoritative full text (playback done,
    // or TTS died). No-op if nothing is parked — e.g. barge-in already
    // cleared it, so the partial spoken text stays put.
    if (parkedReply === null) return
    const text = parkedReply
    parkedReply = null
    const bubble = ensureReplyBubble()
    bubble.text = text
    opts.displayedAgentText.value = text
  }

  function resetVoiceReply() {
    stopReveal()
    replyBubbleId = null
    parkedReply = null
    audioStarted = false
  }

  /** Open a fresh voice turn: freezes/detaches the previous bubble and pushes the user bubble. */
  function beginVoiceTurn(text: string) {
    voiceTurnId++
    resetVoiceReply()
    clearTtsWatchdog()
    opts.messages.value.push({ id: `voice-user-${voiceTurnId}`, role: "user", text })
  }

  function startTtsWatchdog() {
    clearTtsWatchdog()
    ttsWatchdog = window.setTimeout(() => {
      ttsWatchdog = null
      applyParkedReply()
      opts.onWatchdogExpire()
    }, TTS_WATCHDOG_MS)
  }

  function parkReply(text: string) {
    parkedReply = text
  }

  /** Barge-in: discard the parked reply without touching bubble/turn bookkeeping. */
  function discardParkedReply() {
    parkedReply = null
  }

  function markAudioStarted() {
    audioStarted = true
  }

  function hasAudioStarted() {
    return audioStarted
  }

  return {
    beginVoiceTurn,
    stopReveal,
    revealSegment,
    applyParkedReply,
    resetVoiceReply,
    startTtsWatchdog,
    clearTtsWatchdog,
    parkReply,
    discardParkedReply,
    markAudioStarted,
    hasAudioStarted,
  }
}
