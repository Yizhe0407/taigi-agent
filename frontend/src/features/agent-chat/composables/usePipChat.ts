import {
  computed,
  nextTick,
  onBeforeUnmount,
  ref,
  type Ref,
  useTemplateRef,
  watch,
} from "vue"

import { UI_FALLBACK_MESSAGES } from "@/lib/api-messages"

import {
  ChatApiError,
  createChatSession,
  deleteChatSession,
  sendChatMessage,
} from "../api/chat"
import type { PipChatMessage } from "../types"

let messageCounter = 0

function nextMessageId(): string {
  messageCounter += 1
  return `${Date.now()}-${messageCounter}`
}

export function usePipChat(isOpen: Readonly<Ref<boolean>>) {
  const sessionId = ref<string | null>(null)
  const messages = ref<PipChatMessage[]>([])
  const userInput = ref("")
  const isSending = ref(false)
  const showChat = ref(false)
  const chatBodyRef = useTemplateRef<HTMLElement>("pip-chat-body")

  async function ensureSession() {
    if (sessionId.value) return

    try {
      sessionId.value = await createChatSession()
      if (!messages.value.length) {
        messages.value = [
          {
            id: "welcome",
            role: "assistant",
            text: "您好，我是小芸。請問需要什麼幫忙呢？",
          },
        ]
      }
    } catch {
      if (!messages.value.length) {
        messages.value = [
          {
            id: "session-error",
            role: "assistant",
            text: UI_FALLBACK_MESSAGES.agentOffline,
          },
        ]
      }
    }
  }

  async function scrollToBottom() {
    await nextTick()
    if (chatBodyRef.value) {
      chatBodyRef.value.scrollTop = chatBodyRef.value.scrollHeight
    }
  }

  async function sendMessage() {
    const text = userInput.value.trim()
    if (!text || isSending.value) return

    await ensureSession()
    if (!sessionId.value) return

    userInput.value = ""
    const id = nextMessageId()
    messages.value.push({ id, role: "user", text })
    await scrollToBottom()

    isSending.value = true
    try {
      const reply = await sendChatMessage(sessionId.value, text)
      messages.value.push({ id: `${id}-reply`, role: "assistant", text: reply })
    } catch (error) {
      const message =
        error instanceof ChatApiError
          ? error.message
          : UI_FALLBACK_MESSAGES.agentNoReply
      messages.value.push({
        id: `${id}-error`,
        role: "assistant",
        text: `（${message}）`,
      })
    } finally {
      isSending.value = false
      await scrollToBottom()
    }
  }

  function handleKeydown(event: KeyboardEvent) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault()
      void sendMessage()
    }
  }

  function toggleChat() {
    showChat.value = !showChat.value
    if (showChat.value) void ensureSession()
  }

  const lastAgentText = computed(() => {
    const lastMessage = [...messages.value]
      .reverse()
      .find((message) => message.role === "assistant")
    return lastMessage?.text ?? "小芸正在等待您的問題"
  })

  watch(
    isOpen,
    (open) => {
      if (open) void ensureSession()
    },
    { immediate: true },
  )

  onBeforeUnmount(() => {
    if (sessionId.value) void deleteChatSession(sessionId.value)
  })

  return {
    messages,
    userInput,
    isSending,
    showChat,
    lastAgentText,
    ensureSession,
    sendMessage,
    handleKeydown,
    toggleChat,
  }
}
