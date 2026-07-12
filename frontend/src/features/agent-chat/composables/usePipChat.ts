import {
  nextTick,
  onBeforeUnmount,
  ref,
  type Ref,
  useTemplateRef,
} from "vue"

import { UI_FALLBACK_MESSAGES } from "@/lib/api-messages"

import {
  ChatApiError,
  createChatSession,
  deleteChatSession,
  deleteChatSessionBeacon,
  sendChatMessageStream,
} from "../api/chat"
import type { PipChatMessage } from "../types"
import { useTts } from "./useTts"

let messageCounter = 0

function nextMessageId(): string {
  messageCounter += 1
  return `${Date.now()}-${messageCounter}`
}

/**
 * Text-chat side of the PiP. The overlay drives its lifecycle explicitly
 * (`ensureSession` on open, `endSession` on close) — this composable no longer
 * watches `open` itself, so the teardown order lives in one place (see
 * PipAgentOverlay's single open-watcher).
 *
 * `onActivity` is the shared idle-timer signal: every text interaction (typing,
 * send, each streamed reply chunk) feeds the same `markActivity` the voice
 * events use, so text and voice keep the PiP alive identically.
 */
export function usePipChat(
  suppressTts: Readonly<Ref<boolean>> = ref(false),
  onActivity: () => void = () => {},
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

  // ponytail: in-flight promise reuse so two concurrent callers share one POST
  let _ensureSessionInflight: Promise<void> | null = null
  // Bumped by reset(): if the panel closes while a createChatSession() POST is
  // in flight, the resolved id is orphaned server-side (leaks until TTL) and a
  // stale sessionId gets resurrected. Comparing generations discards it and
  // deletes the just-created session instead.
  let sessionGeneration = 0

  async function ensureSession(): Promise<void> {
    if (sessionId.value) return
    if (_ensureSessionInflight) return _ensureSessionInflight
    const generation = sessionGeneration
    _ensureSessionInflight = (async () => {
      try {
        const id = await createChatSession()
        if (generation !== sessionGeneration) {
          // Panel was reset while this POST was in flight — abandon the session.
          void deleteChatSession(id)
          return
        }
        sessionId.value = id
        // Voice mode (suppressTts): the pipeline announces the welcome itself over
        // the data channel — text and audio arrive together. Only greet locally
        // in text-only mode, otherwise the subtitle shows seconds before the voice.
        if (!messages.value.length && !suppressTts.value) {
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
      } finally {
        _ensureSessionInflight = null
      }
    })()
    return _ensureSessionInflight
  }

  async function scrollToBottom() {
    await nextTick()
    if (chatBodyRef.value) {
      chatBodyRef.value.scrollTop = chatBodyRef.value.scrollHeight
    }
  }

  // Abort handle for the in-flight streamed reply — used by teardown so a
  // closed PiP stops pulling SSE (and the backend discards the partial turn).
  let streamAbort: AbortController | null = null

  function abortStream() {
    streamAbort?.abort()
    streamAbort = null
  }

  async function sendMessage() {
    const text = userInput.value.trim()
    if (!text || isSending.value) return

    onActivity()
    await ensureSession()
    if (!sessionId.value) return

    userInput.value = ""
    const id = nextMessageId()
    messages.value.push({ id, role: "user", text })
    await scrollToBottom()

    isSending.value = true
    const replyId = `${id}-reply`
    streamAbort = new AbortController()
    try {
      // Reply streams into the chat bubble as the LLM generates it; the
      // avatar subtitle + TTS still use the full text so audio stays in sync.
      const reply = await sendChatMessageStream(
        sessionId.value,
        text,
        (delta) => {
          onActivity() // a live reply counts as activity — don't idle-close mid-stream
          const bubble = messages.value.find(m => m.id === replyId)
          if (bubble) bubble.text += delta
          else messages.value.push({ id: replyId, role: "assistant", text: delta })
          void scrollToBottom()
        },
        streamAbort.signal,
      )
      if (!messages.value.some(m => m.id === replyId)) {
        messages.value.push({ id: replyId, role: "assistant", text: reply })
      }
      speakWithAnimation(reply)
    } catch (error) {
      // Aborted by teardown — the PiP is closing, don't surface an error bubble.
      if (error instanceof DOMException && error.name === "AbortError") return
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
      streamAbort = null
      isSending.value = false
      await scrollToBottom()
    }
  }

  function handleKeydown(event: KeyboardEvent) {
    onActivity() // any keystroke while typing keeps the PiP alive
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault()
      void sendMessage()
    }
  }

  /** Tear down the text-chat session: stop the stream, drop local state, and
   * DELETE the server session. The single close path (overlay) calls this. */
  function endSession() {
    abortStream()
    sessionGeneration++
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

  // Tab kill / navigation never runs the close path — keepalive DELETE is the
  // only chance to free the server session before the document tears down.
  function handlePageHide(event: PageTransitionEvent) {
    // bfcache freeze is not a teardown — the page may come back with this
    // sessionId still live on the client, so the server row must survive.
    if (event.persisted) return
    if (sessionId.value) deleteChatSessionBeacon(sessionId.value)
  }
  window.addEventListener("pagehide", handlePageHide)

  onBeforeUnmount(() => {
    window.removeEventListener("pagehide", handlePageHide)
    // Safety net if the component unmounts without going through the close path.
    abortStream()
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
    ensureSession,
    sendMessage,
    handleKeydown,
    abortStream,
    endSession,
  }
}
