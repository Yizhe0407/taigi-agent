<script setup lang="ts">
import { LoaderCircle, Send, TriangleAlert } from "@lucide/vue"
import { nextTick, onMounted, onUnmounted, ref } from "vue"

import { Button } from "@/components/ui/button"

import {
  ChatApiError,
  createChatSession,
  deleteChatSession,
  sendChatMessage,
} from "../api/chat"

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
const sendError = ref("")

const scrollAnchor = ref<HTMLElement | null>(null)
const inputRef = ref<HTMLTextAreaElement | null>(null)

let abortController: AbortController | null = null

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

  sendError.value = ""
  userInput.value = ""
  isSending.value = true

  messages.value.push({ id: makeId(), role: "user", content: text })
  await scrollToBottom()

  abortController = new AbortController()
  try {
    const reply = await sendChatMessage(
      sessionId.value,
      text,
      abortController.signal,
    )
    messages.value.push({ id: makeId(), role: "assistant", content: reply })
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") return

    const isExpired =
      error instanceof ChatApiError && error.status === 404
    if (isExpired) {
      // Session expired — clear and try to re-create next turn
      sessionId.value = null
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
  // Enter sends; Shift+Enter inserts newline
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
  if (sessionId.value) {
    deleteChatSession(sessionId.value)
  }
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
      <!-- Welcome hint -->
      <div
        v-if="messages.length === 0 && !sessionError"
        class="flex h-full flex-col items-center justify-center gap-3 text-center"
      >
        <p class="max-w-xs text-sm text-muted-foreground">
          請直接輸入你想詢問的公車資訊，例如「201 下一班幾點？」或「現在還有哪些車？」
        </p>
      </div>

      <template v-for="msg in messages" :key="msg.id">
        <!-- User bubble -->
        <div v-if="msg.role === 'user'" class="flex justify-end">
          <div
            class="max-w-[80%] rounded-2xl rounded-tr-sm bg-primary px-4 py-3 text-sm leading-relaxed text-primary-foreground"
          >
            {{ msg.content }}
          </div>
        </div>

        <!-- Assistant bubble -->
        <div v-else-if="msg.role === 'assistant'" class="flex justify-start">
          <div
            class="max-w-[80%] rounded-2xl rounded-tl-sm border border-border bg-muted/50 px-4 py-3 text-sm leading-relaxed text-foreground"
          >
            {{ msg.content }}
          </div>
        </div>

        <!-- Error bubble -->
        <div v-else class="flex justify-start">
          <div
            class="flex max-w-[80%] items-start gap-2 rounded-2xl rounded-tl-sm border border-destructive/25 bg-destructive/8 px-4 py-3 text-sm text-destructive"
          >
            <TriangleAlert class="mt-0.5 size-4 shrink-0" />
            <span>{{ msg.content }}</span>
          </div>
        </div>
      </template>

      <!-- Typing indicator -->
      <div v-if="isSending" class="flex justify-start">
        <div
          class="flex items-center gap-2 rounded-2xl rounded-tl-sm border border-border bg-muted/50 px-4 py-3"
        >
          <LoaderCircle class="size-4 animate-spin text-muted-foreground" />
          <span class="text-sm text-muted-foreground">助理回應中</span>
        </div>
      </div>

      <!-- Scroll anchor -->
      <div ref="scrollAnchor" />
    </div>

    <!-- Input bar -->
    <div class="shrink-0 border-t border-border bg-background px-4 py-3 sm:px-6">
      <div class="flex items-end gap-3">
        <textarea
          ref="inputRef"
          v-model="userInput"
          rows="1"
          placeholder="輸入問題…"
          class="min-h-[2.75rem] flex-1 resize-none rounded-xl border border-border bg-muted/40 px-4 py-3 text-sm leading-snug text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring disabled:cursor-not-allowed disabled:opacity-50"
          :disabled="isSending || !!sessionError"
          style="field-sizing: content; max-height: 8rem"
          @keydown="onKeydown"
        />
        <Button
          class="size-11 shrink-0 rounded-xl"
          :disabled="!userInput.trim() || isSending || !!sessionError"
          @click="send"
        >
          <Send class="size-4" />
          <span class="sr-only">送出</span>
        </Button>
      </div>
      <p class="mt-2 text-center text-xs text-muted-foreground">
        Enter 送出・Shift+Enter 換行
      </p>
    </div>
  </div>
</template>
