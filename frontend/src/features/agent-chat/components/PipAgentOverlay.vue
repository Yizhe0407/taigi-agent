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

// Streaming voice reply bubble state — reset per turn by onTranscript.
// voiceReplyDone guards against a stray late agent_delta after agent_reply
// already arrived (out-of-order data channel messages) leaving a leftover bubble.
let voiceReplyId: string | null = null
let voiceReplyDone = false

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

onUnmounted(() => { clearIdleTimeout(); clearThinkingFuse() })

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
    voiceReplyId = null
    voiceReplyDone = false
    messages.value.push({ id: `wrtc-user-${Date.now()}`, role: "user", text })
  },
  (text) => {
    // Final full-text signal — overwrite (not append) so it's idempotent
    // whether or not agent_delta chunks already built the bubble.
    clearThinkingFuse()
    if (voiceReplyId) {
      const bubble = messages.value.find(m => m.id === voiceReplyId)
      if (bubble) bubble.text = text
      else messages.value.push({ id: voiceReplyId, role: "assistant", text })
    } else {
      messages.value.push({ id: `wrtc-reply-${Date.now()}`, role: "assistant", text })
    }
    voiceReplyDone = true
    lastUserText.value = ""
    isWebRTCThinking.value = false
    resetIdleTimeout()
    displayedAgentText.value = text
  },
  // Share the existing chat session so voice and text have the same conversation context.
  sessionId,
  () => {
    clearThinkingFuse()
    isWebRTCThinking.value = false
    lastUserText.value = ""
    resetIdleTimeout()
  },
  (delta) => {
    // Ignore stray deltas that arrive after agent_reply already finalized this turn.
    if (voiceReplyDone) return
    clearIdleTimeout()
    clearThinkingFuse() // reply has started streaming — no longer "stalled"
    if (!voiceReplyId) {
      voiceReplyId = `wrtc-reply-${Date.now()}`
      messages.value.push({ id: voiceReplyId, role: "assistant", text: delta })
    } else {
      const bubble = messages.value.find(m => m.id === voiceReplyId)
      if (bubble) bubble.text += delta
    }
    displayedAgentText.value += delta
  },
  // bot_speaking: TTS audio is actively playing — hold off both timers so
  // long replies don't get killed mid-playback.
  () => {
    clearIdleTimeout()
    clearThinkingFuse()
  },
  // bot_silent: TTS finished a chunk — restart the idle clock. agent_reply's
  // own resetIdleTimeout() below remains the fallback if bot_speaking never fires.
  () => {
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
