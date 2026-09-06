import { flushPromises, shallowMount } from "@vue/test-utils"
import { ref } from "vue"
import { afterEach, describe, expect, it, vi } from "vitest"

type HarnessOptions = {
  initialWebRtcState?: "disconnected" | "connecting" | "connected" | "error"
  connectFailure?: Error
  disconnectFailure?: Error
  endSessionFailure?: Error
  disconnectSyncFailure?: Error
  reportFailure?: Error
  resetVoiceReplyFailure?: Error
}

async function installHarness(options: HarnessOptions = {}) {
  vi.resetModules()

  const conversationState = ref("idle")
  const conversation = {
    state: conversationState,
    reset: vi.fn(() => { conversationState.value = "idle" }),
    setConnecting: vi.fn(() => { conversationState.value = "connecting" }),
    setListening: vi.fn(() => { conversationState.value = "listening" }),
    setError: vi.fn(() => { conversationState.value = "error" }),
    forceListening: vi.fn(() => { conversationState.value = "listening" }),
    onTranscript: vi.fn(),
    onAgentCancelled: vi.fn(),
    onSubtitle: vi.fn(),
    onBotSpeaking: vi.fn(),
    onBotSilent: vi.fn(),
    onUserSpeaking: vi.fn(),
    onUserSilent: vi.fn(),
  }

  const ensureSession = vi.fn(async () => ({ id: "session-id" }))
  const endSession = vi.fn(() => options.endSessionFailure
    ? Promise.reject(options.endSessionFailure)
    : Promise.resolve())
  const connect = vi.fn(() => options.connectFailure
    ? Promise.reject(options.connectFailure)
    : Promise.resolve())
  const disconnect = vi.fn(() => {
    if (options.disconnectSyncFailure) throw options.disconnectSyncFailure
    return options.disconnectFailure
      ? Promise.reject(options.disconnectFailure)
      : Promise.resolve()
  })
  const beginVoiceSession = vi.fn(() => true)
  const reportClientEvent = vi.fn(() => {
    if (options.reportFailure) throw options.reportFailure
  })
  const clearThinkingFuse = vi.fn()
  const clearProcessingFuse = vi.fn()
  const resetVoiceReply = vi.fn(() => {
    if (options.resetVoiceReplyFailure) throw options.resetVoiceReplyFailure
  })
  const dismissEndConfirm = vi.fn()
  const resetIdleTimer = vi.fn()
  const pauseIdleTracking = vi.fn()

  vi.doMock("@/features/agent-chat/composables/useConversationState", () => ({
    useConversationState: () => conversation,
  }))
  vi.doMock("@/features/agent-chat/composables/usePipIdleTimer", () => ({
    usePipIdleTimer: () => ({
      showIdleWarning: ref(false),
      idleWarnSecondsLeft: ref(0),
      resetIdleTimer,
      pauseIdleTracking,
      markActivity: vi.fn(),
    }),
  }))
  vi.doMock("@/features/agent-chat/composables/usePipEndConfirm", () => ({
    usePipEndConfirm: () => ({
      showEndConfirm: ref(false),
      endConfirmSecondsLeft: ref(0),
      resolvePending: vi.fn(() => false),
      onEndConversationEvent: vi.fn(),
      dismissEndConfirm,
      confirmEndNow: vi.fn(),
      continueConversation: vi.fn(),
    }),
  }))
  vi.doMock("@/features/agent-chat/composables/usePipChat", () => ({
    usePipChat: () => ({
      messages: ref([]),
      messageWriter: {},
      userInput: ref(""),
      isSending: ref(false),
      showChat: ref(false),
      displayedAgentText: ref(""),
      clearDisplayedText: vi.fn(),
      ttsState: ref("idle"),
      mouthAmplitude: ref(0),
      ensureSession,
      sendMessage: vi.fn(),
      handleKeydown: vi.fn(),
      endSession,
    }),
  }))
  vi.doMock("@/features/agent-chat/composables/usePipVoiceReveal", () => ({
    usePipVoiceReveal: () => ({
      beginVoiceSession,
      beginVoiceTurn: vi.fn(),
      revealSegment: vi.fn(() => true),
      resetVoiceReply,
      receiveReply: vi.fn(() => true),
      cancelVoiceReply: vi.fn(() => true),
      markAudioStarted: vi.fn(() => true),
      finishVoiceReply: vi.fn(() => true),
    }),
  }))
  vi.doMock("@/features/agent-chat/composables/usePipTurnFuses", () => ({
    useThinkingFuse: () => ({ start: vi.fn(), clear: clearThinkingFuse }),
    useProcessingFuse: () => ({ clear: clearProcessingFuse }),
  }))
  vi.doMock("@/features/agent-chat/composables/useWebRTC", () => ({
    useWebRTC: () => ({
      state: ref(options.initialWebRtcState ?? "disconnected"),
      mouthAmplitude: ref(0),
      micDenied: ref(false),
      connect,
      disconnect,
    }),
  }))
  vi.doMock("@/lib/report-client-event", () => ({ reportClientEvent }))

  const { default: PipAgentOverlay } = await import(
    "@/features/agent-chat/components/PipAgentOverlay.vue"
  )
  return {
    PipAgentOverlay,
    conversation,
    ensureSession,
    endSession,
    connect,
    disconnect,
    beginVoiceSession,
    reportClientEvent,
    clearThinkingFuse,
    clearProcessingFuse,
    resetVoiceReply,
    dismissEndConfirm,
    resetIdleTimer,
    pauseIdleTracking,
  }
}

afterEach(() => {
  vi.clearAllMocks()
  vi.resetModules()
})

describe("PipAgentOverlay lifecycle", () => {
  it("forwards an open/connect failure to Vue error handling", async () => {
    const connectFailure = new Error("voice connect failed")
    const harness = await installHarness({ connectFailure })
    const handledErrors: unknown[] = []
    const wrapper = shallowMount(harness.PipAgentOverlay, {
      props: { open: false },
      global: {
        config: {
          errorHandler: error => handledErrors.push(error),
        },
      },
    })

    await wrapper.setProps({ open: true })
    await flushPromises()

    expect(harness.ensureSession).toHaveBeenCalledOnce()
    expect(harness.beginVoiceSession).toHaveBeenCalledOnce()
    expect(harness.connect).toHaveBeenCalledWith("session-id")
    expect(harness.conversation.setError).toHaveBeenCalled()
    expect(harness.reportClientEvent).toHaveBeenCalledWith(
      "pip_overlay_lifecycle_error",
      connectFailure.message,
    )
    expect(handledErrors).toContain(connectFailure)

    wrapper.unmount()
  })


  it("starts chat teardown even when WebRTC disconnect throws synchronously", async () => {
    const disconnectFailure = new Error("synchronous disconnect failure")
    const harness = await installHarness({
      initialWebRtcState: "connected",
      disconnectSyncFailure: disconnectFailure,
    })
    const handledErrors: unknown[] = []
    const wrapper = shallowMount(harness.PipAgentOverlay, {
      props: { open: true },
      global: {
        config: {
          errorHandler: error => handledErrors.push(error),
        },
      },
    })

    await wrapper.setProps({ open: false })
    await flushPromises()

    expect(harness.disconnect).toHaveBeenCalledOnce()
    expect(harness.endSession).toHaveBeenCalledOnce()
    expect(handledErrors).toContain(disconnectFailure)
    wrapper.unmount()
  })

  it("joins asynchronous teardown and surfaces reporter failure after local cleanup fails", async () => {
    const localFailure = new Error("voice reset failed")
    const reportingFailure = new Error("lifecycle reporter failed")
    const harness = await installHarness({
      initialWebRtcState: "connected",
      resetVoiceReplyFailure: localFailure,
      reportFailure: reportingFailure,
    })
    const handledErrors: unknown[] = []
    const wrapper = shallowMount(harness.PipAgentOverlay, {
      props: { open: true },
      global: {
        config: {
          errorHandler: error => handledErrors.push(error),
        },
      },
    })

    await wrapper.setProps({ open: false })
    await flushPromises()

    expect(harness.disconnect).toHaveBeenCalledOnce()
    expect(harness.endSession).toHaveBeenCalledOnce()
    expect(harness.clearThinkingFuse).toHaveBeenCalled()
    expect(harness.clearProcessingFuse).toHaveBeenCalled()
    expect(harness.pauseIdleTracking).toHaveBeenCalled()
    expect(harness.dismissEndConfirm).toHaveBeenCalled()
    expect(handledErrors).toContainEqual(expect.objectContaining({
      message: "PiP overlay lifecycle and failure reporting both failed",
      errors: [localFailure, reportingFailure],
    }))
    wrapper.unmount()
  })

  it("starts both close owners and forwards their aggregate failure to Vue", async () => {
    const disconnectFailure = new Error("WebRTC disconnect failed")
    const endSessionFailure = new Error("chat session cleanup failed")
    const harness = await installHarness({
      initialWebRtcState: "connected",
      disconnectFailure,
      endSessionFailure,
    })
    const handledErrors: unknown[] = []
    const wrapper = shallowMount(harness.PipAgentOverlay, {
      props: { open: true },
      global: {
        config: {
          errorHandler: error => handledErrors.push(error),
        },
      },
    })

    await wrapper.setProps({ open: false })
    await flushPromises()

    expect(harness.disconnect).toHaveBeenCalledOnce()
    expect(harness.endSession).toHaveBeenCalledOnce()
    const aggregate = handledErrors.find(error => error instanceof AggregateError)
    expect(aggregate).toBeInstanceOf(AggregateError)
    expect((aggregate as AggregateError).errors).toEqual([
      disconnectFailure,
      endSessionFailure,
    ])
    expect(harness.reportClientEvent).toHaveBeenCalledWith(
      "pip_overlay_lifecycle_error",
      "PiP overlay teardown failed",
    )
    expect(harness.resetVoiceReply).toHaveBeenCalled()
    expect(harness.clearThinkingFuse).toHaveBeenCalled()
    expect(harness.clearProcessingFuse).toHaveBeenCalled()
    expect(harness.pauseIdleTracking).toHaveBeenCalled()
    expect(harness.dismissEndConfirm).toHaveBeenCalled()

    wrapper.unmount()
  })
})
