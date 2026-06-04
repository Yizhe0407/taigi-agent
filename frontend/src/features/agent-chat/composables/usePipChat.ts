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
import { useTts } from "./useTts"

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
  const { ttsState, mouthAmplitude, speak: speakTts, cancel: cancelTts } = useTts()

  const displayedAgentText = ref("")
  let typewriterToken = 0

  async function animateText(text: string, durationMs?: number) {
    const token = ++typewriterToken
    displayedAgentText.value = ""
    const delay = durationMs ? Math.max(15, durationMs / text.length) : 35
    for (const char of text) {
      if (token !== typewriterToken) return
      displayedAgentText.value += char
      await new Promise<void>(r => setTimeout(r, delay))
    }
  }

  function speakWithAnimation(text: string) {
    void speakTts(text).then(durationMs => void animateText(text, durationMs ?? undefined))
  }

  function clearDisplayedText() {
    typewriterToken++
    displayedAgentText.value = ""
  }

  async function ensureSession() {
    if (sessionId.value) return

    try {
      sessionId.value = await createChatSession()
      if (!messages.value.length) {
        const welcomeText = "要去哪？"
        messages.value = [{ id: "welcome", role: "assistant", text: welcomeText }]
        speakWithAnimation(welcomeText)
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
      speakWithAnimation(reply)
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
      void animateText(`（${message}）`)
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

  async function sendVoiceMessage(text: string) {
    const message = text.trim()
    if (!message || isSending.value) return

    await ensureSession()
    if (!sessionId.value) return

    const id = nextMessageId()
    messages.value.push({ id, role: "user", text: message })
    await scrollToBottom()

    isSending.value = true
    try {
      const reply = await sendChatMessage(sessionId.value, message)
      messages.value.push({ id: `${id}-reply`, role: "assistant", text: reply })
      speakWithAnimation(reply)
    }
    catch (error) {
      const msg = error instanceof ChatApiError ? error.message : UI_FALLBACK_MESSAGES.agentNoReply
      messages.value.push({ id: `${id}-error`, role: "assistant", text: `（${msg}）` })
      void animateText(`（${msg}）`)
    }
    finally {
      isSending.value = false
      await scrollToBottom()
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
    return lastMessage?.text ?? "要去哪？"
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
    displayedAgentText,
    clearDisplayedText,
    ttsState,
    mouthAmplitude,
    cancelTts,
    ensureSession,
    sendMessage,
    sendVoiceMessage,
    handleKeydown,
    toggleChat,
  }
}
