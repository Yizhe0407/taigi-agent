import {
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

export function usePipChat(
  isOpen: Readonly<Ref<boolean>>,
  suppressTts: Readonly<Ref<boolean>> = ref(false),
) {
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
    if (suppressTts.value) {
      // WebRTC is active — TTS is handled by the voice pipeline.
      // Only run the typewriter animation, don't call REST TTS.
      void animateText(text)
    } else {
      void speakTts(text).then(durationMs => void animateText(text, durationMs ?? undefined))
    }
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
        const welcomeText = "請問您欲前往哪裡？"
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

  function reset() {
    typewriterToken++
    displayedAgentText.value = ""
    cancelTts()
    isSending.value = false
    showChat.value = false
    messages.value = []
    if (sessionId.value) {
      void deleteChatSession(sessionId.value)
      sessionId.value = null
    }
  }

  watch(
    isOpen,
    (open) => {
      if (open) void ensureSession()
      else reset()
    },
    { immediate: true },
  )

  onBeforeUnmount(() => {
    // Safety net if component unmounts without going through close path
    if (sessionId.value) void deleteChatSession(sessionId.value)
  })

  return {
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
    sendMessage,
    handleKeydown,
  }
}
