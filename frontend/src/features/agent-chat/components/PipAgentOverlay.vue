<script setup lang="ts">
import { computed, ref, toRef, watch, onUnmounted } from "vue"

import { usePipChat } from "@/features/agent-chat/composables/usePipChat"
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
const isWebRTCThinking = ref(false)

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
  ttsWatchdog = window.setTimeout(() => {
    ttsWatchdog = null
    flushPendingReply()
  }, 4000)
}

let idleTimeout: number | null = null

function clearIdleTimeout() {
  if (idleTimeout) {
    clearTimeout(idleTimeout)
    idleTimeout = null
  }
}

function resetIdleTimeout() {
  clearIdleTimeout()
  idleTimeout = window.setTimeout(() => {
    emit('close')
  }, 60000)
}

// 30s safety fuse: if no reply/cancelled arrives, auto-reset thinking state
let thinkingFuse: number | null = null

function clearThinkingFuse() {
  if (thinkingFuse !== null) { clearTimeout(thinkingFuse); thinkingFuse = null }
}

function startThinkingFuse() {
  clearThinkingFuse()
  thinkingFuse = window.setTimeout(() => {
    isWebRTCThinking.value = false
    lastUserText.value = ""
    resetIdleTimeout()
  }, 30_000)
}

onUnmounted(() => { clearIdleTimeout(); clearThinkingFuse(); clearTtsWatchdog(); stopSubtitleReveal() })

const {
  state: webrtcState,
  mouthAmplitude: webrtcAmplitude,
  connect: webrtcConnect,
  disconnect: webrtcDisconnect,
} = useWebRTC(
  (text) => {
    lastUserText.value = text
    clearDisplayedText()
    clearIdleTimeout()
    isWebRTCThinking.value = true
    startThinkingFuse()
    stopSubtitleReveal()
    voiceReplyId = null
    pendingReply = null
    clearTtsWatchdog()
    messages.value.push({ id: `wrtc-user-${Date.now()}`, role: "user", text })
  },
  (text) => {
    // Full-text signal — timing relative to playback varies (see scenarios
    // 1-3 above the state declarations). Always just park it and arm the
    // watchdog; subtitle/bot_speaking will cancel the watchdog if audio is
    // actually playing, bot_silent (normal case) or the watchdog (TTS-failed
    // case) does the actual flush.
    clearThinkingFuse()
    lastUserText.value = ""
    isWebRTCThinking.value = false
    resetIdleTimeout()
    pendingReply = text
    startTtsWatchdog()
  },
  // Share the existing chat session so voice and text have the same conversation context.
  sessionId,
  () => {
    // agent_cancelled (barge-in): leave the bubble/subtitle as-is, stopped at
    // whatever was actually spoken — completing it here would show text the
    // user never heard.
    clearThinkingFuse()
    isWebRTCThinking.value = false
    lastUserText.value = ""
    resetIdleTimeout()
    clearTtsWatchdog()
    stopSubtitleReveal()
  },
  (text, durationMs) => {
    // subtitle: fires as each TTS segment starts playing (see pipeline.py
    // SubtitleSyncProcessor) — reveals over durationMs so the bubble tracks
    // playback pace instead of dumping the whole segment at once.
    // Audio is alive, so the TTS-failed watchdog no longer applies.
    clearTtsWatchdog()
    startSubtitleReveal(text, durationMs)
  },
  // bot_speaking: TTS audio is actively playing — hold off both timers so
  // long replies don't get killed mid-playback.
  () => {
    clearTtsWatchdog()
    clearIdleTimeout()
    clearThinkingFuse()
  },
  // bot_silent: playback finished. Stop any still-running reveal loop first —
  // flushPendingReply() overwrites the bubble/displayedAgentText outright, and
  // a stale animateSubtitle() timer must not keep appending after that.
  // Flush the full-text reply the server sent earlier (idempotently covers
  // any subtitle gaps) and restart the idle clock.
  () => {
    clearTtsWatchdog()
    stopSubtitleReveal()
    flushPendingReply()
    resetIdleTimeout()
  },
)


// Use WebRTC audio amplitude when connected, otherwise TTS amplitude
const activeMouthAmplitude = computed(() =>
  webrtcState.value === "connected" ? webrtcAmplitude.value : mouthAmplitude.value
)

function toggleVoice() {
  if (webrtcState.value === "disconnected" || webrtcState.value === "error") {
    cancelTts()
    clearDisplayedText()
    void webrtcConnect()
  } else {
    // connected or connecting → abort/disconnect
    webrtcDisconnect()
  }
}

// Map WebRTC state → VoiceState for PipFrame's visual indicators.
// 'recording' keeps the pip-listening border animation active whenever we're live.
const voiceState = computed<"idle" | "recording" | "transcribing">(() => {
  if (webrtcState.value === "connecting") return "transcribing"
  if (webrtcState.value === "connected") return "recording"
  return "idle"
})

watch(
  webrtcState,
  (state) => {
    suppressTts.value = state === "connected" || state === "connecting"
  },
  { immediate: true }
)

watch(
  () => props.open,
  async (open) => {
    if (open) {
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
      resetIdleTimeout()
    } else {
      showChat.value = false
      moveMode.value = false
      settingsMode.value = false
      cancelTts()
      clearDisplayedText()
      stopSubtitleReveal()
      webrtcDisconnect()
      lastUserText.value = ""
      isWebRTCThinking.value = false
      clearIdleTimeout()
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
        :is-sending="isSending || isWebRTCThinking"
        :show-chat="showChat"
        :voice-state="voiceState"
        :tts-state="ttsState"
        :mouth-amplitude="activeMouthAmplitude"
        :webrtc-state="webrtcState"
        :last-user-text="lastUserText"
        :move-mode="moveMode"
        :settings-mode="settingsMode"
        :corner="corner"
        @close="emit('close')"
        @toggle-voice="toggleVoice"
        @toggle-settings="settingsMode = !settingsMode"
        @cancel-settings="settingsMode = false"
        @set-size="size = $event"
        @enter-move="enterMoveFromSettings"
        @open-chat="openChatFromSettings"
        @select-corner="selectCorner"
        @cancel-move="moveMode = false"
      />
    </div>
  </Transition>
</template>
