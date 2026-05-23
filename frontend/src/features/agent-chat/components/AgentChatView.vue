<script setup lang="ts">
import { LoaderCircle, Mic, MicOff, Send, TriangleAlert } from "@lucide/vue"
import { nextTick, onMounted, onUnmounted, ref } from "vue"

import { Button } from "@/components/ui/button"

import {
  ChatApiError,
  createChatSession,
  deleteChatSession,
  sendChatMessage,
  transcribeAudio,
} from "../api/chat"
import { blobToWav } from "../composables/encodeWav"
import { useAudioRecorder } from "../composables/useAudioRecorder"

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

type MessageRole = "user" | "assistant" | "error"

interface ChatMessage {
  id: string
  role: MessageRole
  content: string
}

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

const sessionId = ref<string | null>(null)
const sessionError = ref("")
const messages = ref<ChatMessage[]>([])
const userInput = ref("")
const isSending = ref(false)

const scrollAnchor = ref<HTMLElement | null>(null)
const inputRef = ref<HTMLTextAreaElement | null>(null)

let abortController: AbortController | null = null

// ---------------------------------------------------------------------------
// Audio recorder (callback API — auto-resets to idle after onAudioReady)
// ---------------------------------------------------------------------------

const {
  state: recorderState,
  error: recorderError,
  volume: recorderVolume,
  startRecording,
  stopRecording,
  reset: resetRecorder,
} = useAudioRecorder({
  async onAudioReady(blob) {
    // Re-create session if it expired while the user was idle / recording.
    // Done here so the session is guaranteed fresh when the user hits Send.
    if (!sessionId.value) await initSession()

    try {
      const wav = await blobToWav(blob)
      userInput.value = await transcribeAudio(wav)
      await nextTick()
      inputRef.value?.focus()
    } catch (e) {
      const msg = e instanceof ChatApiError ? e.message : "語音辨識失敗"
      messages.value.push({ id: makeId(), role: "error", content: msg })
      await scrollToBottom()
    }
    // composable auto-transitions to idle after this resolves/rejects
  },
})

function toggleRecording() {
  if (recorderState.value === "recording") stopRecording()
  else if (recorderState.value === "idle") startRecording()
}

// Height (px) for each of the 5 volume bars, scaled by current RMS energy.
// Shape profile peaks in the middle: [0.5, 0.75, 1.0, 0.75, 0.5].
// Min height 3px so bars are always visible when recording.
const BAR_PROFILES = [0.5, 0.75, 1.0, 0.75, 0.5]
function barHeight(profile: number): string {
  return `${Math.max(3, recorderVolume.value * profile * 26)}px`
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function makeId() {
  return `${Date.now()}-${Math.random().toString(36).slice(2, 7)}`
}

async function scrollToBottom() {
  await nextTick()
  scrollAnchor.value?.scrollIntoView({ behavior: "smooth" })
}

// ---------------------------------------------------------------------------
// Session lifecycle
// ---------------------------------------------------------------------------

async function initSession() {
  sessionError.value = ""
  try {
    sessionId.value = await createChatSession()
  } catch (error) {
    sessionError.value =
      error instanceof ChatApiError ? error.message : "助理服務暫時無法使用"
  }
}

// ---------------------------------------------------------------------------
// Send message
// ---------------------------------------------------------------------------

async function send() {
  const text = userInput.value.trim()
  if (!text || isSending.value) return

  // If session expired, re-create before sending
  if (!sessionId.value) {
    await initSession()
    if (!sessionId.value) return
  }

  userInput.value = ""
  isSending.value = true

  messages.value.push({ id: makeId(), role: "user", content: text })
  await scrollToBottom()

  abortController = new AbortController()
  try {
    const reply = await sendChatMessage(sessionId.value, text, abortController.signal)
    messages.value.push({ id: makeId(), role: "assistant", content: reply })
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") return

    if (error instanceof ChatApiError && error.status === 404) {
      sessionId.value = null // session expired — re-create next turn
    }

    const msg =
      error instanceof ChatApiError ? error.message : "助理暫時無法回應，請稍後再試"
    messages.value.push({ id: makeId(), role: "error", content: msg })
  } finally {
    isSending.value = false
    abortController = null
    await scrollToBottom()
    await nextTick()
    inputRef.value?.focus()
  }
}

function onKeydown(event: KeyboardEvent) {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault()
    send()
  }
}

// ---------------------------------------------------------------------------
// Lifecycle
// ---------------------------------------------------------------------------

onMounted(initSession)

onUnmounted(() => {
  abortController?.abort()
  resetRecorder()
  if (sessionId.value) deleteChatSession(sessionId.value)
})
</script>

<template>
  <div class="flex h-full min-h-0 flex-col">
    <!-- Session init error -->
    <div
      v-if="sessionError"
      class="flex items-start gap-3 border-b border-destructive/20 bg-destructive/8 px-5 py-4 text-sm text-destructive"
    >
      <TriangleAlert class="mt-0.5 size-4 shrink-0" />
      <span>{{ sessionError }}</span>
      <button
        type="button"
        class="ml-auto shrink-0 text-xs underline underline-offset-2"
        @click="initSession"
      >
        重試
      </button>
    </div>

    <!-- Message list -->
    <div class="flex-1 space-y-4 overflow-y-auto px-4 py-5 sm:px-6">
      <div
        v-if="messages.length === 0 && !sessionError"
        class="flex h-full flex-col items-center justify-center gap-3 text-center"
      >
        <p class="max-w-xs text-sm text-muted-foreground">
          請直接輸入你想詢問的公車資訊，例如「201 下一班幾點？」或「現在還有哪些車？」
        </p>
      </div>

      <template v-for="msg in messages" :key="msg.id">
        <div v-if="msg.role === 'user'" class="flex justify-end">
          <div class="max-w-[80%] rounded-2xl rounded-tr-sm bg-primary px-4 py-3 text-sm leading-relaxed text-primary-foreground">
            {{ msg.content }}
          </div>
        </div>

        <div v-else-if="msg.role === 'assistant'" class="flex justify-start">
          <div class="max-w-[80%] rounded-2xl rounded-tl-sm border border-border bg-muted/50 px-4 py-3 text-sm leading-relaxed text-foreground">
            {{ msg.content }}
          </div>
        </div>

        <div v-else class="flex justify-start">
          <div class="flex max-w-[80%] items-start gap-2 rounded-2xl rounded-tl-sm border border-destructive/25 bg-destructive/8 px-4 py-3 text-sm text-destructive">
            <TriangleAlert class="mt-0.5 size-4 shrink-0" />
            <span>{{ msg.content }}</span>
          </div>
        </div>
      </template>

      <!-- Typing indicator -->
      <div v-if="isSending" class="flex justify-start">
        <div class="flex items-center gap-2 rounded-2xl rounded-tl-sm border border-border bg-muted/50 px-4 py-3">
          <LoaderCircle class="size-4 animate-spin text-muted-foreground" />
          <span class="text-sm text-muted-foreground">助理回應中</span>
        </div>
      </div>

      <div ref="scrollAnchor" />
    </div>

    <!-- Input bar -->
    <div class="shrink-0 border-t border-border bg-background px-4 py-3 sm:px-6">
      <!-- Mic permission / recorder error -->
      <div
        v-if="recorderError"
        class="mb-2 flex items-center gap-2 rounded-lg border border-destructive/25 bg-destructive/8 px-3 py-2 text-xs text-destructive"
      >
        <TriangleAlert class="size-3.5 shrink-0" />
        <span>{{ recorderError }}</span>
      </div>

      <div class="flex items-end gap-2">
        <!-- Mic button — pulse ring appears while recording -->
        <div class="relative shrink-0">
          <span
            v-if="recorderState === 'recording'"
            class="pointer-events-none absolute inset-0 animate-ping rounded-xl bg-destructive/25"
          />
          <Button
            variant="ghost"
            class="relative size-11 rounded-xl transition-colors"
            :class="{
              'bg-destructive/10 text-destructive hover:bg-destructive/20': recorderState === 'recording',
              'text-muted-foreground': recorderState === 'idle',
            }"
            :disabled="isSending || !!sessionError || recorderState === 'processing'"
            :aria-label="recorderState === 'recording' ? '停止錄音' : '開始錄音'"
            @click="toggleRecording"
          >
            <LoaderCircle v-if="recorderState === 'processing'" class="size-4 animate-spin" />
            <MicOff v-else-if="recorderState === 'recording'" class="size-4" />
            <Mic v-else class="size-4" />
          </Button>
        </div>

        <!-- Textarea slot: volume bars while recording, normal textarea otherwise -->
        <div class="flex-1">
          <!-- Volume bars (recording) -->
          <div
            v-if="recorderState === 'recording'"
            class="flex h-[2.75rem] items-end justify-center gap-1.5 rounded-xl border border-destructive/30 bg-destructive/5 pb-2.5"
          >
            <div
              v-for="(profile, i) in BAR_PROFILES"
              :key="i"
              class="w-1.5 rounded-full bg-destructive/70 transition-[height] duration-75"
              :style="{ height: barHeight(profile) }"
            />
          </div>

          <!-- Processing state -->
          <div
            v-else-if="recorderState === 'processing'"
            class="flex h-[2.75rem] items-center gap-2 rounded-xl border border-border bg-muted/40 px-4"
          >
            <LoaderCircle class="size-3.5 animate-spin text-muted-foreground" />
            <span class="text-sm text-muted-foreground">辨識中…</span>
          </div>

          <!-- Normal textarea (idle) -->
          <textarea
            v-else
            ref="inputRef"
            v-model="userInput"
            rows="1"
            placeholder="輸入問題，或點麥克風說話…"
            class="min-h-[2.75rem] w-full resize-none rounded-xl border border-border bg-muted/40 px-4 py-3 text-sm leading-snug text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring disabled:cursor-not-allowed disabled:opacity-50"
            :disabled="isSending || !!sessionError"
            style="field-sizing: content; max-height: 8rem"
            @keydown="onKeydown"
          />
        </div>

        <!-- Send button -->
        <Button
          class="size-11 shrink-0 rounded-xl"
          :disabled="!userInput.trim() || isSending || !!sessionError || recorderState !== 'idle'"
          @click="send"
        >
          <Send class="size-4" />
          <span class="sr-only">送出</span>
        </Button>
      </div>

      <p class="mt-2 text-center text-xs text-muted-foreground">
        Enter 送出・Shift+Enter 換行・麥克風說台語
      </p>
    </div>
  </div>
</template>
