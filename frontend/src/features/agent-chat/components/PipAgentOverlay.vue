<script setup lang="ts">
import { computed, ref, toRef, watch, onUnmounted } from "vue"

import { usePipChat } from "@/features/agent-chat/composables/usePipChat"
import { useConversationState } from "@/features/agent-chat/composables/useConversationState"
import { useWebRTC } from "@/features/agent-chat/composables/useWebRTC"

import PipChatPanel from "./PipChatPanel.vue"
import PipFrame from "./PipFrame.vue"
import { PIP_SIZES, type PipCorner, type PipSize } from "../types"

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ close: [] }>()

const corner = ref<PipCorner>("br")
const size = ref<PipSize>("lg")
const moveMode = ref(false)
const settingsMode = ref(false)

const suppressTts = ref(false)

const {
  sessionId,
  messages,
  userInput,
  isSending,
  showChat,
  displayedAgentText,
  clearDisplayedText,
  ttsState,
  mouthAmplitude,
  cancelTts,
  ensureSession,
  sendMessage,
  handleKeydown,
} = usePipChat(toRef(props, "open"), suppressTts)

const lastUserText = ref("")

// Single source of truth for the voice conversation phase — see
// useConversationState for the transition table and defensive handling.
const conversation = useConversationState()

// Playback-synced voice reply bubble state — reset per turn by onTranscript.
// voiceReplyId: bubble built up from `subtitle` events as TTS audio plays.
// pendingReply: full-text `agent_reply` is *always* parked here, never shown
// immediately — only `bot_silent` (or the TTS watchdog below) applies it to
// the bubble, so the transcript never gets ahead of the audio. Three timings:
//   1. Normal long reply: subtitle/bot_speaking arrive first (clear the
//      watchdog), agent_reply lands mid-playback and is parked, bot_silent
//      flushes it once playback ends.
//   2. Canned short reply (near-zero LLM latency): agent_reply arrives
//      *before* any subtitle/bot_speaking. It's still just parked — no
//      immediate-display branch — so the bubble stays empty until subtitle
//      starts appending; bot_silent flushes the full text at the end.
//      Nothing ever flashes full-text-then-retypes.
//   3. TTS failure: no subtitle/bot_speaking ever fires this turn, so the
//      4s watchdog started alongside pendingReply fires and flushes it
//      itself — the one case where the bubble fills outside of bot_silent.
let voiceReplyId: string | null = null
let pendingReply: string | null = null
let ttsWatchdog: number | null = null

function clearTtsWatchdog() {
  if (ttsWatchdog !== null) { clearTimeout(ttsWatchdog); ttsWatchdog = null }
}

// Subtitle reveal: each `subtitle` event carries one segment's text plus its
// audio duration. We reveal it char-by-char over that duration (rhythm mirrors
// usePipChat's animateText, but state lives here since voice owns its own bubble).
// subtitleToken guards the in-flight setTimeout loop; subtitleRemaining tracks
// how much of the *current* segment hasn't been appended yet.
let subtitleToken = 0
let subtitleRemaining: { text: string; index: number } | null = null

function flushSubtitleReveal() {
  // A new segment arrived while the previous one was still revealing — audio
  // has already moved on, so instantly finish the old segment's text rather
  // than let the subtitle lag behind.
  if (!subtitleRemaining) return
  const rest = subtitleRemaining.text.slice(subtitleRemaining.index)
  subtitleRemaining = null
  if (!rest || !voiceReplyId) return
  const bubble = messages.value.find(m => m.id === voiceReplyId)
  if (bubble) bubble.text += rest
  displayedAgentText.value += rest
}

function stopSubtitleReveal() {
  // Interruption / new turn: stop without flushing — leave the bubble at
  // whatever was actually spoken (completing it here would show text the
  // user never heard).
  subtitleToken++
  subtitleRemaining = null
}

async function animateSubtitle(text: string, durationMs: number, token: number) {
  const delay = text.length ? Math.max(15, durationMs / text.length) : 0
  for (let i = 0; i < text.length; i++) {
    if (token !== subtitleToken) return // superseded by a newer segment, or stopped
    const bubble = voiceReplyId ? messages.value.find(m => m.id === voiceReplyId) : null
    if (bubble) bubble.text += text[i]
    displayedAgentText.value += text[i]
    subtitleRemaining = { text, index: i + 1 }
    if (delay > 0) await new Promise<void>(r => setTimeout(r, delay))
  }
  if (token === subtitleToken) subtitleRemaining = null
}

function startSubtitleReveal(text: string, durationMs: number) {
  flushSubtitleReveal() // finish whatever segment was still revealing
  const token = ++subtitleToken
  if (!voiceReplyId) {
    voiceReplyId = `wrtc-reply-${Date.now()}`
    messages.value.push({ id: voiceReplyId, role: "assistant", text: "" })
  }
  void animateSubtitle(text, durationMs, token)
}

function flushPendingReply() {
  if (pendingReply === null) return
  const text = pendingReply
  pendingReply = null
  if (voiceReplyId) {
    const bubble = messages.value.find(m => m.id === voiceReplyId)
    if (bubble) bubble.text = text
    else messages.value.push({ id: voiceReplyId, role: "assistant", text })
  } else {
    voiceReplyId = `wrtc-reply-${Date.now()}`
    messages.value.push({ id: voiceReplyId, role: "assistant", text })
  }
  displayedAgentText.value = text
}

function startTtsWatchdog() {
  clearTtsWatchdog()
  // TTS-specific failure: the LLM answered (pendingReply is set) but audio
  // never started (no subtitle/bot_speaking). Flush the text so it's not
  // lost, and hand the state machine back — nothing is coming to leave
  // `thinking` otherwise.
  ttsWatchdog = window.setTimeout(() => {
    ttsWatchdog = null
    flushPendingReply()
    conversation.forceListening()
    // If end_conversation already arrived (waiting on a bot_silent that will
    // never come because TTS died), surface the card now instead of never.
    if (!resolvePendingEndConversation()) resetIdleTimer()
  }, 4000)
}

// 30s safety fuse: total-failure fallback if neither a reply nor a cancel
// ever arrives for a turn (distinct from the 4s TTS watchdog above, which
// only fires once a reply text already exists) — force back to listening
// rather than leaving the UI stuck in `thinking`.
let thinkingFuse: number | null = null

function clearThinkingFuse() {
  if (thinkingFuse !== null) { clearTimeout(thinkingFuse); thinkingFuse = null }
}

function startThinkingFuse() {
  clearThinkingFuse()
  thinkingFuse = window.setTimeout(() => {
    thinkingFuse = null
    lastUserText.value = ""
    conversation.forceListening()
    // Same fallback as the TTS watchdog: a parked end_conversation must still
    // get its card even when the farewell turn silently died.
    if (!resolvePendingEndConversation()) resetIdleTimer()
  }, 30_000)
}

// Processing fuse: `user_silent` -> `transcript` has no guaranteed follow-up —
// backend drops empty ASR results without emitting any event — so "辨識中…"
// could otherwise hang until the 45s idle path. Armed/cleared by watching the
// state itself: any transition out of `processing` (transcript, barge-in,
// error, reset) disarms it.
const PROCESSING_FUSE_MS = 10_000
let processingFuse: number | null = null

function clearProcessingFuse() {
  if (processingFuse !== null) { clearTimeout(processingFuse); processingFuse = null }
}

watch(
  () => conversation.state.value,
  (state) => {
    clearProcessingFuse()
    if (state === "processing") {
      processingFuse = window.setTimeout(() => {
        processingFuse = null
        conversation.forceListening()
        resetIdleTimer()
      }, PROCESSING_FUSE_MS)
    }
  },
)

// ---- Idle timer: 45s of no activity -> warning card w/ 15s countdown ->
// zero closes. Any voice event or touch (see markActivity) cancels and
// returns to the prior state. Replaces the old blind 60s auto-close.
const IDLE_WARN_AFTER_MS = 45_000
const IDLE_WARN_COUNTDOWN_S = 15

const showIdleWarning = ref(false)
const idleWarnSecondsLeft = ref(IDLE_WARN_COUNTDOWN_S)
let idleTimer: number | null = null
let idleWarnInterval: number | null = null

function clearIdleTimer() {
  if (idleTimer !== null) { clearTimeout(idleTimer); idleTimer = null }
}

function clearIdleWarnInterval() {
  if (idleWarnInterval !== null) { clearInterval(idleWarnInterval); idleWarnInterval = null }
}

function dismissIdleWarning() {
  showIdleWarning.value = false
  clearIdleWarnInterval()
}

function startIdleWarning() {
  showIdleWarning.value = true
  idleWarnSecondsLeft.value = IDLE_WARN_COUNTDOWN_S
  clearIdleWarnInterval()
  idleWarnInterval = window.setInterval(() => {
    idleWarnSecondsLeft.value -= 1
    if (idleWarnSecondsLeft.value <= 0) {
      clearIdleWarnInterval()
      emit("close")
    }
  }, 1000)
}

function resetIdleTimer() {
  clearIdleTimer()
  dismissIdleWarning()
  idleTimer = window.setTimeout(startIdleWarning, IDLE_WARN_AFTER_MS)
}

/** Any voice event or touch resets the 45s idle clock and cancels the warning card. */
function markActivity() {
  resetIdleTimer()
}

// ---- End-of-conversation confirm card. Backend emits `end_conversation` at
// tool-round time — the spoken farewell is generated by the *next* LLM round,
// so the event almost always arrives while we're still in `thinking` (or
// `processing`/`speaking`). Two timings to cover:
//   - mid-turn (thinking/processing/userSpeaking/speaking): park it and wait
//     for bot_silent so the card never covers or cuts off the farewell audio;
//     the thinking-fuse / TTS-watchdog fallbacks surface it if audio dies.
//   - late (already back to `listening`): show immediately.
const END_CONFIRM_COUNTDOWN_S = 10

const showEndConfirm = ref(false)
const endConfirmSecondsLeft = ref(END_CONFIRM_COUNTDOWN_S)
let endConfirmInterval: number | null = null
let endConversationPending = false

function clearEndConfirmInterval() {
  if (endConfirmInterval !== null) { clearInterval(endConfirmInterval); endConfirmInterval = null }
}

function showEndConversationCard() {
  clearIdleTimer()
  dismissIdleWarning()
  showEndConfirm.value = true
  endConfirmSecondsLeft.value = END_CONFIRM_COUNTDOWN_S
  clearEndConfirmInterval()
  endConfirmInterval = window.setInterval(() => {
    endConfirmSecondsLeft.value -= 1
    if (endConfirmSecondsLeft.value <= 0) {
      clearEndConfirmInterval()
      emit("close")
    }
  }, 1000)
}

/** Show the parked end-conversation card, if any. Returns whether it did. */
function resolvePendingEndConversation(): boolean {
  if (!endConversationPending) return false
  endConversationPending = false
  showEndConversationCard()
  return true
}

function onEndConversationEvent() {
  if (conversation.state.value === "listening") {
    showEndConversationCard()
  } else {
    endConversationPending = true
  }
}

function dismissEndConfirm() {
  clearEndConfirmInterval()
  showEndConfirm.value = false
  endConversationPending = false
}

function confirmEndNow() {
  clearEndConfirmInterval()
  showEndConfirm.value = false
  emit("close")
}

function continueConversation() {
  dismissEndConfirm()
  conversation.setListening()
  resetIdleTimer()
}

onUnmounted(() => {
  clearIdleTimer()
  clearThinkingFuse()
  clearProcessingFuse()
  clearTtsWatchdog()
  clearIdleWarnInterval()
  clearEndConfirmInterval()
  stopSubtitleReveal()
})

const {
  state: webrtcState,
  mouthAmplitude: webrtcAmplitude,
  connect: webrtcConnect,
  disconnect: webrtcDisconnect,
} = useWebRTC(
  {
    onTranscript: (text) => {
      lastUserText.value = text
      clearDisplayedText()
      conversation.onTranscript()
      markActivity()
      startThinkingFuse()
      stopSubtitleReveal()
      voiceReplyId = null
      pendingReply = null
      clearTtsWatchdog()
      messages.value.push({ id: `wrtc-user-${Date.now()}`, role: "user", text })
    },
    onReply: (text) => {
      // Full-text signal — timing relative to playback varies (see scenarios
      // 1-3 above the state declarations). Always just park it and arm the
      // watchdog; subtitle/bot_speaking will cancel the watchdog if audio is
      // actually playing, bot_silent (normal case) or the watchdog (TTS-failed
      // case) does the actual flush.
      clearThinkingFuse()
      lastUserText.value = ""
      markActivity()
      pendingReply = text
      startTtsWatchdog()
    },
    onCancelled: () => {
      // agent_cancelled (barge-in): leave the bubble/subtitle as-is, stopped at
      // whatever was actually spoken — completing it here would show text the
      // user never heard.
      clearThinkingFuse()
      lastUserText.value = ""
      conversation.onAgentCancelled()
      markActivity()
      clearTtsWatchdog()
      stopSubtitleReveal()
    },
    onSubtitle: (text, durationMs) => {
      // fires as each TTS segment starts playing (see pipeline.py
      // SubtitleSyncProcessor) — reveals over durationMs so the bubble tracks
      // playback pace instead of dumping the whole segment at once.
      // Audio is alive, so the TTS-failed watchdog no longer applies.
      clearTtsWatchdog()
      conversation.onSubtitle()
      markActivity()
      startSubtitleReveal(text, durationMs)
    },
    onBotSpeaking: () => {
      // TTS audio is actively playing — pause the idle clock entirely (not
      // just reschedule) so long replies don't get killed mid-playback.
      clearTtsWatchdog()
      clearThinkingFuse()
      conversation.onBotSpeaking()
      clearIdleTimer()
      dismissIdleWarning()
    },
    onBotSilent: () => {
      // Playback finished. Stop any still-running reveal loop first —
      // flushPendingReply() overwrites the bubble/displayedAgentText outright, and
      // a stale animateSubtitle() timer must not keep appending after that.
      clearTtsWatchdog()
      stopSubtitleReveal()
      flushPendingReply()
      conversation.onBotSilent()
      if (!resolvePendingEndConversation()) resetIdleTimer()
    },
    onUserSpeaking: () => {
      // User spoke while the end-confirm card was up — they clearly want to
      // keep going; treat it as pressing「閣繼續問」so the 10s countdown can't
      // close the PiP mid-sentence.
      if (showEndConfirm.value) dismissEndConfirm()
      conversation.onUserSpeaking()
      markActivity()
    },
    onUserSilent: () => {
      conversation.onUserSilent()
      markActivity()
    },
    onEndConversation: onEndConversationEvent,
  },
  // Share the existing chat session so voice and text have the same conversation context.
  sessionId,
)


// Use WebRTC audio amplitude when connected, otherwise TTS amplitude
const activeMouthAmplitude = computed(() =>
  webrtcState.value === "connected" ? webrtcAmplitude.value : mouthAmplitude.value
)

watch(
  webrtcState,
  (state) => {
    suppressTts.value = state === "connected" || state === "connecting"
    if (state === "connecting") conversation.setConnecting()
    else if (state === "connected") {
      conversation.setListening()
      resetIdleTimer()
    } else if (props.open) {
      // "error", or an unexpected "disconnected" while the PiP is still open
      // (deliberate close paths flip props.open false *before* disconnecting,
      // so they never reach here). Freeze into the static error visual and
      // kill every event-driven timer — no data-channel event can arrive to
      // resolve them. The idle timer stays armed as the automatic exit next
      // to the always-visible end button.
      conversation.setError()
      clearThinkingFuse()
      clearProcessingFuse()
      clearTtsWatchdog()
      dismissEndConfirm()
      resetIdleTimer()
    }
  },
  { immediate: true }
)

watch(
  () => props.open,
  async (open) => {
    if (open) {
      conversation.reset()
      if (webrtcState.value === "connecting") webrtcDisconnect()
      if (webrtcState.value !== "connected") {
        // Suppress REST TTS before ensureSession: the voice pipeline owns the
        // welcome greeting. Without this the welcome is spoken twice (REST +
        // WebRTC). The webrtcState watcher takes over after connect starts.
        suppressTts.value = true
        await ensureSession() // wait for session before sending offer (ensureSession never throws)
        if (!props.open) return // closed while awaiting session
        void webrtcConnect()
      }
    } else {
      showChat.value = false
      moveMode.value = false
      settingsMode.value = false
      cancelTts()
      clearDisplayedText()
      stopSubtitleReveal()
      webrtcDisconnect()
      lastUserText.value = ""
      clearThinkingFuse()
      clearProcessingFuse()
      clearTtsWatchdog()
      clearIdleTimer()
      clearIdleWarnInterval()
      clearEndConfirmInterval()
      showIdleWarning.value = false
      showEndConfirm.value = false
      endConversationPending = false
      conversation.reset()
    }
  },
)

const frameSize = computed(() => PIP_SIZES[size.value])

function selectCorner(next: PipCorner) {
  corner.value = next
  moveMode.value = false
}

function enterMoveFromSettings() {
  settingsMode.value = false
  moveMode.value = true
}

function openChatFromSettings() {
  settingsMode.value = false
  showChat.value = true
}

// Class-based positioning — Tailwind utilities derived from corner. Avoids inline-style reactivity issues with Transition.
const posClass = computed(() => {
  switch (corner.value) {
    case "tl": return "top-5 left-5"
    case "tr": return "top-5 right-5"
    case "bl": return "bottom-5 left-5"
    case "br": return "bottom-5 right-5"
  }
})
const dirClass = computed(() =>
  corner.value.endsWith("r") ? "flex-row-reverse" : "flex-row",
)
</script>

<template>
  <Transition
    enter-active-class="transition-[opacity,transform] duration-200"
    enter-from-class="opacity-0 scale-95"
    leave-active-class="transition-[opacity,transform] duration-200"
    leave-to-class="opacity-0 scale-95"
  >
    <div
      v-if="open"
      class="fixed z-[9999] flex gap-3 items-end max-w-[calc(100vw-40px)]"
      :class="[posClass, dirClass]"
      @pointerdown.capture="markActivity"
    >
      <Transition
        enter-active-class="transition-[opacity,transform] duration-[180ms]"
        enter-from-class="opacity-0 translate-x-2"
        leave-active-class="transition-[opacity,transform] duration-[180ms]"
        leave-to-class="opacity-0 translate-x-2"
      >
        <PipChatPanel
          v-if="showChat"
          v-model:user-input="userInput"
          :messages="messages"
          :is-sending="isSending"
          :height-px="frameSize.h"
          @close="showChat = false"
          @send="sendMessage"
          @keydown="handleKeydown"
        />
      </Transition>

      <PipFrame
        :width="frameSize.w"
        :height="frameSize.h"
        :size="size"
        :last-agent-text="displayedAgentText"
        :is-sending="isSending"
        :show-chat="showChat"
        :conversation-state="conversation.state.value"
        :tts-state="ttsState"
        :mouth-amplitude="activeMouthAmplitude"
        :last-user-text="lastUserText"
        :move-mode="moveMode"
        :settings-mode="settingsMode"
        :corner="corner"
        :show-end-confirm="showEndConfirm"
        :end-confirm-seconds-left="endConfirmSecondsLeft"
        :show-idle-warning="showIdleWarning"
        :idle-warn-seconds-left="idleWarnSecondsLeft"
        @close="emit('close')"
        @toggle-settings="settingsMode = !settingsMode"
        @cancel-settings="settingsMode = false"
        @set-size="size = $event"
        @enter-move="enterMoveFromSettings"
        @open-chat="openChatFromSettings"
        @select-corner="selectCorner"
        @cancel-move="moveMode = false"
        @confirm-end="confirmEndNow"
        @continue-conversation="continueConversation"
        @dismiss-idle-warning="() => { dismissIdleWarning(); resetIdleTimer() }"
      />
    </div>
  </Transition>
</template>
