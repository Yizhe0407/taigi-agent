import { effectScope, ref } from "vue"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import {
  createChatSession,
  deleteChatSession,
  sendChatMessageStream,
} from "@/features/agent-chat/api/chat"
import { usePipChat } from "@/features/agent-chat/composables/usePipChat"
import { settleRetainedAsynchronousTeardowns } from "@/lib/component-lifecycle"

const ttsMocks = vi.hoisted(() => ({
  speak: vi.fn<(text: string) => Promise<number | null>>(),
  release: vi.fn<() => Promise<void>>(),
  ttsState: { value: "idle" },
  mouthAmplitude: { value: 0 },
}))

vi.mock("@/features/agent-chat/api/chat", async (importOriginal) => {
  const actual = await importOriginal<
    typeof import("@/features/agent-chat/api/chat")
  >()
  return {
    ...actual,
    createChatSession: vi.fn(),
    deleteChatSession: vi.fn(async () => {}),
    sendChatMessageStream: vi.fn(),
  }
})

vi.mock("@/lib/report-client-event", () => ({
  reportClientEvent: vi.fn(),
}))

vi.mock("@/features/agent-chat/composables/useTts", () => ({
  useTts: () => ({
    ttsState: ttsMocks.ttsState,
    mouthAmplitude: ttsMocks.mouthAmplitude,
    speak: ttsMocks.speak,
    release: ttsMocks.release,
  }),
}))

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}

function pageHideEvent(persisted = false): PageTransitionEvent {
  const event = new Event("pagehide") as PageTransitionEvent
  Object.defineProperty(event, "persisted", { value: persisted })
  return event
}

describe("usePipChat lifecycle ownership", () => {
  beforeEach(() => {
    vi.mocked(createChatSession).mockReset()
    vi.mocked(deleteChatSession).mockReset().mockResolvedValue()
    vi.mocked(sendChatMessageStream).mockReset()
    ttsMocks.speak.mockReset().mockResolvedValue(null)
    ttsMocks.release.mockReset().mockResolvedValue()
    ttsMocks.ttsState.value = "idle"
    ttsMocks.mouthAmplitude.value = 0
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
  })

  it("joins a pending create before deleting its client ID and opening a replacement generation", async () => {
    const oldId = "019d0000-0000-7000-8000-000000000001"
    const replacementId = "019d0000-0000-7000-8000-000000000002"
    const oldCreation = deferred<void>()
    vi.spyOn(globalThis.crypto, "randomUUID")
      .mockReturnValueOnce(oldId)
      .mockReturnValueOnce(replacementId)
    vi.mocked(createChatSession)
      .mockImplementationOnce(() => oldCreation.promise)
      .mockResolvedValueOnce()

    const scope = effectScope()
    const chat = scope.run(() => usePipChat(ref(true)))
    expect(chat).toBeDefined()

    chat!.userInput.value = "old-session-text"
    const staleSend = chat!.sendMessage()
    await vi.waitFor(() =>
      expect(createChatSession).toHaveBeenCalledWith(oldId, expect.any(AbortSignal)),
    )

    const closing = chat!.endSession()
    const replacement = chat!.ensureSession()
    expect(deleteChatSession).not.toHaveBeenCalled()

    oldCreation.resolve()
    await Promise.all([staleSend, closing])
    await expect(replacement).resolves.toEqual({ id: replacementId, generation: 1 })

    expect(
      vi.mocked(deleteChatSession).mock.calls.filter(([sessionId]) => sessionId === oldId),
    ).toHaveLength(1)
    expect(sendChatMessageStream).not.toHaveBeenCalled()

    scope.stop()
    await chat!.endSession()
    expect(
      vi.mocked(deleteChatSession).mock.calls.filter(([sessionId]) => sessionId === replacementId),
    ).toHaveLength(1)
  })

  it("invalidates an opening generation when close arrives before its first await", async () => {
    const scope = effectScope()
    const chat = scope.run(() => usePipChat(ref(true)))
    expect(chat).toBeDefined()

    const opening = chat!.ensureSession()
    const closing = chat!.endSession()

    await closing
    await expect(opening).resolves.toBeNull()
    expect(createChatSession).not.toHaveBeenCalled()
    expect(deleteChatSession).not.toHaveBeenCalled()

    scope.stop()
    await chat!.endSession()
  })

  it("deletes an abandoned client ID after a non-abort create failure", async () => {
    const sessionId = "019d0000-0000-7000-8000-000000000003"
    vi.spyOn(globalThis.crypto, "randomUUID").mockReturnValue(sessionId)
    vi.mocked(createChatSession).mockRejectedValue(new Error("response lost"))

    const scope = effectScope()
    const chat = scope.run(() => usePipChat(ref(true)))
    expect(chat).toBeDefined()

    await expect(chat!.ensureSession()).resolves.toBeNull()
    expect(deleteChatSession).toHaveBeenCalledOnce()
    expect(deleteChatSession).toHaveBeenCalledWith(sessionId)

    await chat!.endSession()
    scope.stop()
    await chat!.endSession()
    expect(deleteChatSession).toHaveBeenCalledOnce()
  })

  it("joins repeated close and scope disposal through one physical teardown", async () => {
    const sessionId = "019d0000-0000-7000-8000-000000000004"
    vi.spyOn(globalThis.crypto, "randomUUID").mockReturnValue(sessionId)
    vi.mocked(createChatSession).mockResolvedValue()

    const scope = effectScope()
    const chat = scope.run(() => usePipChat(ref(true)))
    expect(chat).toBeDefined()
    await expect(chat!.ensureSession()).resolves.toEqual({ id: sessionId, generation: 0 })

    await Promise.all([chat!.endSession(), chat!.endSession()])
    scope.stop()
    await chat!.endSession()

    expect(deleteChatSession).toHaveBeenCalledOnce()
    expect(deleteChatSession).toHaveBeenCalledWith(sessionId)
    expect(ttsMocks.release).not.toHaveBeenCalled()
  })

  it("routes pagehide, explicit close, and scope disposal through one deletion owner", async () => {
    const sessionId = "019d0000-0000-7000-8000-000000000005"
    vi.spyOn(globalThis.crypto, "randomUUID").mockReturnValue(sessionId)
    vi.mocked(createChatSession).mockResolvedValue()

    const scope = effectScope()
    const chat = scope.run(() => usePipChat(ref(true)))
    expect(chat).toBeDefined()
    await chat!.ensureSession()

    window.dispatchEvent(pageHideEvent())
    await chat!.endSession()
    scope.stop()
    await chat!.endSession()

    expect(deleteChatSession).toHaveBeenCalledOnce()
    expect(deleteChatSession).toHaveBeenCalledWith(sessionId)
    expect(ttsMocks.release).not.toHaveBeenCalled()
  })

  it("joins scope disposal that arrives during pagehide teardown without releasing TTS twice", async () => {
    const sessionId = "019d0000-0000-7000-8000-000000000015"
    const release = deferred<void>()
    vi.spyOn(globalThis.crypto, "randomUUID").mockReturnValue(sessionId)
    vi.mocked(createChatSession).mockResolvedValue()
    ttsMocks.release.mockImplementation(() => release.promise)
    const removeEventListener = vi.spyOn(window, "removeEventListener")

    const scope = effectScope()
    const chat = scope.run(() => usePipChat(ref(false)))
    expect(chat).toBeDefined()
    await chat!.ensureSession()
    await vi.waitFor(() => expect(ttsMocks.speak).toHaveBeenCalledOnce())

    window.dispatchEvent(pageHideEvent())
    await vi.waitFor(() => expect(ttsMocks.release).toHaveBeenCalledOnce())

    const closing = chat!.endSession()
    scope.stop()
    expect(ttsMocks.release).toHaveBeenCalledOnce()

    release.resolve()
    await closing
    await chat!.endSession()

    expect(ttsMocks.release).toHaveBeenCalledOnce()
    expect(deleteChatSession).toHaveBeenCalledOnce()
    expect(removeEventListener).toHaveBeenCalledWith("pagehide", expect.any(Function))
  })

  it("preserves a live session across bfcache pagehide", async () => {
    const sessionId = "019d0000-0000-7000-8000-000000000006"
    vi.spyOn(globalThis.crypto, "randomUUID").mockReturnValue(sessionId)
    vi.mocked(createChatSession).mockResolvedValue()

    const scope = effectScope()
    const chat = scope.run(() => usePipChat(ref(true)))
    expect(chat).toBeDefined()
    await chat!.ensureSession()

    window.dispatchEvent(pageHideEvent(true))
    expect(deleteChatSession).not.toHaveBeenCalled()

    scope.stop()
    await chat!.endSession()
    expect(deleteChatSession).toHaveBeenCalledOnce()
    expect(deleteChatSession).toHaveBeenCalledWith(sessionId)
  })

  it("owns concurrent sends before session creation yields", async () => {
    const sessionId = "019d0000-0000-7000-8000-000000000007"
    const creation = deferred<void>()
    vi.spyOn(globalThis.crypto, "randomUUID").mockReturnValue(sessionId)
    vi.mocked(createChatSession).mockImplementation(() => creation.promise)
    vi.mocked(sendChatMessageStream).mockResolvedValue("reply")

    const scope = effectScope()
    const chat = scope.run(() => usePipChat(ref(true)))
    expect(chat).toBeDefined()
    chat!.userInput.value = "only once"

    const first = chat!.sendMessage()
    const second = chat!.sendMessage()
    expect(chat!.isSending.value).toBe(true)

    await vi.waitFor(() => expect(createChatSession).toHaveBeenCalledOnce())
    creation.resolve()
    await Promise.all([first, second])

    expect(sendChatMessageStream).toHaveBeenCalledOnce()
    expect(sendChatMessageStream).toHaveBeenCalledWith(
      sessionId,
      "only once",
      expect.any(Function),
      expect.any(AbortSignal),
    )
    expect(chat!.messages.value.filter(message => message.role === "user")).toHaveLength(1)

    scope.stop()
    await chat!.endSession()
  })

  it("aborts but retains an active send until its physical stream operation settles", async () => {
    const sessionId = "019d0000-0000-7000-8000-000000000008"
    const stream = deferred<string>()
    vi.spyOn(globalThis.crypto, "randomUUID").mockReturnValue(sessionId)
    vi.mocked(createChatSession).mockResolvedValue()
    vi.mocked(sendChatMessageStream).mockImplementation(() => stream.promise)

    const scope = effectScope()
    const chat = scope.run(() => usePipChat(ref(true)))
    expect(chat).toBeDefined()
    chat!.userInput.value = "wait for cleanup"

    const sending = chat!.sendMessage()
    await vi.waitFor(() => expect(sendChatMessageStream).toHaveBeenCalledOnce())
    const signal = vi.mocked(sendChatMessageStream).mock.calls[0]?.[3]

    const closing = chat!.endSession()
    expect(signal?.aborted).toBe(true)
    expect(deleteChatSession).not.toHaveBeenCalled()

    stream.resolve("late reply")
    await Promise.all([sending, closing])
    expect(deleteChatSession).toHaveBeenCalledWith(sessionId)
    expect(chat!.messages.value).toHaveLength(0)
    expect(chat!.displayedAgentText.value).toBe("")

    scope.stop()
    await chat!.endSession()
  })

  it("publishes a send owner before AbortController construction can re-enter close", async () => {
    const NativeAbortController = globalThis.AbortController
    let constructionCount = 0
    let closeOnConstruction = Number.POSITIVE_INFINITY
    let reentrantClose: Promise<void> | null = null
    let chat: ReturnType<typeof usePipChat> | undefined

    class ReentrantAbortController extends NativeAbortController {
      constructor() {
        super()
        constructionCount++
        if (constructionCount === closeOnConstruction) {
          reentrantClose = chat!.endSession()
        }
      }
    }

    vi.stubGlobal("AbortController", ReentrantAbortController)
    const scope = effectScope()
    chat = scope.run(() => usePipChat(ref(true)))
    expect(chat).toBeDefined()

    // One controller backs the component lifetime owner. A send then creates
    // its cancellation owner followed by the request controller itself.
    closeOnConstruction = constructionCount + 2
    chat!.userInput.value = "close during acquisition"
    const sending = chat!.sendMessage()

    await sending
    await reentrantClose
    expect(createChatSession).not.toHaveBeenCalled()
    expect(sendChatMessageStream).not.toHaveBeenCalled()
    expect(chat!.isSending.value).toBe(false)

    scope.stop()
    await chat!.endSession()
  })

  it("surfaces and retries a mutate-then-throw active-send abort before deleting once", async () => {
    const sessionId = "019d0000-0000-7000-8000-000000000018"
    const stream = deferred<string>()
    const abortFailure = new Error("send abort failed after mutating")
    const nativeAbort = globalThis.AbortController.prototype.abort
    const failingSignals = new WeakSet<AbortSignal>()
    const abortAttempts = new WeakMap<AbortSignal, number>()
    vi.spyOn(globalThis.AbortController.prototype, "abort").mockImplementation(function (
      this: AbortController,
      reason?: unknown,
    ) {
      const attempt = (abortAttempts.get(this.signal) ?? 0) + 1
      abortAttempts.set(this.signal, attempt)
      nativeAbort.call(this, reason)
      if (failingSignals.has(this.signal) && attempt === 1) throw abortFailure
    })
    vi.spyOn(globalThis.crypto, "randomUUID").mockReturnValue(sessionId)
    vi.mocked(createChatSession).mockResolvedValue()
    vi.mocked(sendChatMessageStream).mockImplementation(() => stream.promise)

    const scope = effectScope()
    const chat = scope.run(() => usePipChat(ref(true)))
    expect(chat).toBeDefined()
    chat!.userInput.value = "join the physical send"

    const sending = chat!.sendMessage()
    await vi.waitFor(() => expect(sendChatMessageStream).toHaveBeenCalledOnce())
    const sendSignal = vi.mocked(sendChatMessageStream).mock.calls[0]?.[3]
    expect(sendSignal).toBeDefined()
    failingSignals.add(sendSignal!)

    const closing = chat!.endSession()
    expect(sendSignal!.aborted).toBe(true)
    expect(deleteChatSession).not.toHaveBeenCalled()

    stream.resolve("late reply")
    await sending
    await expect(closing).rejects.toThrow("PiP chat teardown failed")
    expect(abortAttempts.get(sendSignal!)).toBe(1)
    expect(deleteChatSession).toHaveBeenCalledOnce()

    await expect(chat!.endSession()).resolves.toBeUndefined()
    expect(abortAttempts.get(sendSignal!)).toBe(2)
    expect(deleteChatSession).toHaveBeenCalledOnce()
    scope.stop()
    await chat!.endSession()
  })

  it("surfaces a mutate-then-throw session abort and crosses its retry barrier without a second DELETE", async () => {
    const oldId = "019d0000-0000-7000-8000-000000000019"
    const replacementId = "019d0000-0000-7000-8000-000000000020"
    const abortFailure = new Error("session abort failed after mutating")
    const nativeAbort = globalThis.AbortController.prototype.abort
    const failingSignals = new WeakSet<AbortSignal>()
    const abortAttempts = new WeakMap<AbortSignal, number>()
    vi.spyOn(globalThis.AbortController.prototype, "abort").mockImplementation(function (
      this: AbortController,
      reason?: unknown,
    ) {
      const attempt = (abortAttempts.get(this.signal) ?? 0) + 1
      abortAttempts.set(this.signal, attempt)
      nativeAbort.call(this, reason)
      if (failingSignals.has(this.signal) && attempt === 1) throw abortFailure
    })
    vi.spyOn(globalThis.crypto, "randomUUID")
      .mockReturnValueOnce(oldId)
      .mockReturnValueOnce(replacementId)
    vi.mocked(createChatSession).mockResolvedValue()

    const scope = effectScope()
    const chat = scope.run(() => usePipChat(ref(true)))
    expect(chat).toBeDefined()
    await chat!.ensureSession()
    const sessionSignal = vi.mocked(createChatSession).mock.calls[0]?.[1]
    expect(sessionSignal).toBeDefined()
    failingSignals.add(sessionSignal!)

    await expect(chat!.endSession()).rejects.toThrow("PiP chat teardown failed")
    expect(abortAttempts.get(sessionSignal!)).toBe(1)
    expect(
      vi.mocked(deleteChatSession).mock.calls.filter(([id]) => id === oldId),
    ).toHaveLength(1)

    await expect(chat!.ensureSession()).resolves.toEqual({
      id: replacementId,
      generation: 1,
    })
    expect(abortAttempts.get(sessionSignal!)).toBe(2)
    expect(
      vi.mocked(deleteChatSession).mock.calls.filter(([id]) => id === oldId),
    ).toHaveLength(1)

    scope.stop()
    await chat!.endSession()
  })

  it("ignores a stale TTS completion after a newer reply owns the animation", async () => {
    vi.useFakeTimers()
    const sessionId = "019d0000-0000-7000-8000-000000000009"
    const suppressTts = ref(true)
    const firstSpeech = deferred<number | null>()
    const secondSpeech = deferred<number | null>()
    vi.spyOn(globalThis.crypto, "randomUUID").mockReturnValue(sessionId)
    vi.mocked(createChatSession).mockResolvedValue()
    vi.mocked(sendChatMessageStream)
      .mockResolvedValueOnce("first reply")
      .mockResolvedValueOnce("second reply")
    ttsMocks.speak
      .mockImplementationOnce(() => firstSpeech.promise)
      .mockImplementationOnce(() => secondSpeech.promise)

    const scope = effectScope()
    const chat = scope.run(() => usePipChat(suppressTts))
    expect(chat).toBeDefined()
    await chat!.ensureSession()
    suppressTts.value = false

    chat!.userInput.value = "first"
    await chat!.sendMessage()
    chat!.userInput.value = "second"
    await chat!.sendMessage()

    secondSpeech.resolve(180)
    await Promise.resolve()
    await vi.runAllTimersAsync()
    expect(chat!.displayedAgentText.value).toBe("second reply")

    firstSpeech.resolve(180)
    await Promise.resolve()
    await vi.runAllTimersAsync()
    expect(chat!.displayedAgentText.value).toBe("second reply")

    scope.stop()
    await chat!.endSession()
    expect(vi.getTimerCount()).toBe(0)
  })

  it("keeps exactly one pending typewriter timer when a reply is superseded", async () => {
    vi.useFakeTimers()
    const sessionId = "019d0000-0000-7000-8000-000000000010"
    vi.spyOn(globalThis.crypto, "randomUUID").mockReturnValue(sessionId)
    vi.mocked(createChatSession).mockResolvedValue()
    vi.mocked(sendChatMessageStream)
      .mockResolvedValueOnce("first reply")
      .mockResolvedValueOnce("second reply")

    const scope = effectScope()
    const chat = scope.run(() => usePipChat(ref(true)))
    expect(chat).toBeDefined()

    chat!.userInput.value = "first"
    await chat!.sendMessage()
    expect(vi.getTimerCount()).toBe(1)

    chat!.userInput.value = "second"
    await chat!.sendMessage()
    expect(vi.getTimerCount()).toBe(1)

    scope.stop()
    await chat!.endSession()
    expect(vi.getTimerCount()).toBe(0)
  })

  it("retains the exact typewriter timer when clear fails and gates its late callback", async () => {
    const sessionId = "019d0000-0000-7000-8000-000000000016"
    const scheduled = new Map<number, () => void>()
    let nextTimerId = 1
    const setTimeout = vi.spyOn(window, "setTimeout").mockImplementation(
      ((callback: TimerHandler) => {
        const id = nextTimerId++
        scheduled.set(id, callback as () => void)
        return id
      }) as typeof window.setTimeout,
    )
    const clearTimeout = vi.spyOn(globalThis, "clearTimeout").mockImplementation(
      ((id: number | undefined) => {
        if (id !== undefined) scheduled.delete(id)
      }) as typeof globalThis.clearTimeout,
    )
    clearTimeout.mockImplementationOnce(() => {
      throw new Error("clear typewriter failed")
    })
    vi.spyOn(globalThis.crypto, "randomUUID").mockReturnValue(sessionId)
    vi.mocked(createChatSession).mockResolvedValue()
    vi.mocked(sendChatMessageStream).mockResolvedValue("reply")

    const scope = effectScope()
    const chat = scope.run(() => usePipChat(ref(true)))
    expect(chat).toBeDefined()

    chat!.userInput.value = "message"
    await chat!.sendMessage()
    expect(chat!.displayedAgentText.value).toBe("r")
    expect(setTimeout).toHaveBeenCalledOnce()
    expect([...scheduled.keys()]).toEqual([1])

    expect(() => chat!.clearDisplayedText()).toThrow("clear typewriter failed")
    expect(clearTimeout.mock.calls.map(([id]) => id)).toEqual([1])
    expect(setTimeout).toHaveBeenCalledOnce()
    expect(chat!.displayedAgentText.value).toBe("r")

    expect(() => chat!.clearDisplayedText()).not.toThrow()
    expect(clearTimeout.mock.calls.map(([id]) => id)).toEqual([1, 1])
    expect(setTimeout).toHaveBeenCalledOnce()
    expect(chat!.displayedAgentText.value).toBe("")

    const cachedCallback = scheduled.get(1)
    cachedCallback?.()
    expect(chat!.displayedAgentText.value).toBe("")
    expect(setTimeout).toHaveBeenCalledOnce()

    scope.stop()
    await chat!.endSession()
  })

  it("retries the same pagehide listener after removal fails and gates the cached handler", async () => {
    const sessionId = "019d0000-0000-7000-8000-000000000017"
    const addEventListener = vi.spyOn(window, "addEventListener")
    const removeEventListener = vi.spyOn(window, "removeEventListener")
    removeEventListener.mockImplementationOnce(() => {
      throw new Error("remove pagehide failed")
    })
    vi.spyOn(globalThis.crypto, "randomUUID").mockReturnValue(sessionId)
    vi.mocked(createChatSession).mockResolvedValue()

    const scope = effectScope()
    const chat = scope.run(() => usePipChat(ref(true)))
    expect(chat).toBeDefined()
    await chat!.ensureSession()

    const pagehideHandler = addEventListener.mock.calls.find(
      ([type]) => type === "pagehide",
    )?.[1] as EventListener | undefined
    expect(pagehideHandler).toBeDefined()

    scope.stop()
    await expect(chat!.endSession()).rejects.toThrow("PiP chat teardown failed")
    await expect(chat!.endSession()).resolves.toBeUndefined()

    const removals = removeEventListener.mock.calls.filter(([type]) => type === "pagehide")
    expect(removals).toHaveLength(2)
    expect(removals[0]?.[1]).toBe(pagehideHandler)
    expect(removals[1]?.[1]).toBe(pagehideHandler)
    expect(deleteChatSession).toHaveBeenCalledOnce()
    expect(ttsMocks.release).not.toHaveBeenCalled()

    pagehideHandler?.(pageHideEvent())
    await chat!.endSession()
    expect(deleteChatSession).toHaveBeenCalledOnce()
    expect(ttsMocks.release).not.toHaveBeenCalled()
  })

  it("rolls back a pagehide listener when registration mutates then throws", () => {
    const originalAddEventListener = window.addEventListener.bind(window)
    const addEventListener = vi.spyOn(window, "addEventListener").mockImplementation(
      ((type: string, listener: EventListenerOrEventListenerObject, options?: boolean | AddEventListenerOptions) => {
        originalAddEventListener(type, listener, options)
        throw new Error("pagehide registration failed")
      }) as typeof window.addEventListener,
    )
    const removeEventListener = vi.spyOn(window, "removeEventListener")
    const scope = effectScope()

    expect(() => scope.run(() => usePipChat(ref(true)))).toThrow(
      "pagehide registration failed",
    )

    const pagehideHandler = addEventListener.mock.calls.find(
      ([type]) => type === "pagehide",
    )?.[1]
    expect(removeEventListener).toHaveBeenCalledWith("pagehide", pagehideHandler)
    expect(createChatSession).not.toHaveBeenCalled()

    scope.stop()
    if (typeof pagehideHandler === "function") pagehideHandler(pageHideEvent())
    expect(deleteChatSession).not.toHaveBeenCalled()
    expect(ttsMocks.release).not.toHaveBeenCalled()
  })

  it("retains pagehide registration rollback debt until a distinct teardown attempt", async () => {
    const originalAddEventListener = window.addEventListener.bind(window)
    const originalRemoveEventListener = window.removeEventListener.bind(window)
    const addEventListener = vi.spyOn(window, "addEventListener").mockImplementation(
      ((type: string, listener: EventListenerOrEventListenerObject, options?: boolean | AddEventListenerOptions) => {
        originalAddEventListener(type, listener, options)
        throw new Error("pagehide registration failed")
      }) as typeof window.addEventListener,
    )
    const removeEventListener = vi.spyOn(window, "removeEventListener")
      .mockImplementationOnce(() => {
        throw new Error("pagehide rollback failed")
      })
      .mockImplementation(originalRemoveEventListener)
    const scope = effectScope()

    let setupFailure: unknown
    try {
      scope.run(() => usePipChat(ref(true)))
    } catch (error) {
      setupFailure = error
    }

    expect(setupFailure).toBeInstanceOf(AggregateError)
    expect((setupFailure as AggregateError).errors).toEqual([
      expect.objectContaining({ message: "pagehide registration failed" }),
      expect.objectContaining({ message: "pagehide rollback failed" }),
    ])

    const pagehideHandler = addEventListener.mock.calls.find(
      ([type]) => type === "pagehide",
    )?.[1]
    expect(removeEventListener).toHaveBeenCalledTimes(1)

    scope.stop()
    await expect(settleRetainedAsynchronousTeardowns()).resolves.toBeUndefined()

    const removals = removeEventListener.mock.calls.filter(([type]) => type === "pagehide")
    expect(removals).toHaveLength(2)
    expect(removals[0]?.[1]).toBe(pagehideHandler)
    expect(removals[1]?.[1]).toBe(pagehideHandler)

    if (typeof pagehideHandler === "function") pagehideHandler(pageHideEvent())
    await expect(settleRetainedAsynchronousTeardowns()).resolves.toBeUndefined()
    expect(createChatSession).not.toHaveBeenCalled()
    expect(deleteChatSession).not.toHaveBeenCalled()
    expect(ttsMocks.release).not.toHaveBeenCalled()
  })

  it("retains a failed DELETE owner and retries it before a replacement session", async () => {
    const oldId = "019d0000-0000-7000-8000-000000000011"
    const replacementId = "019d0000-0000-7000-8000-000000000012"
    vi.spyOn(globalThis.crypto, "randomUUID")
      .mockReturnValueOnce(oldId)
      .mockReturnValueOnce(replacementId)
    vi.mocked(createChatSession).mockResolvedValue()
    vi.mocked(deleteChatSession)
      .mockRejectedValueOnce(new Error("delete failed"))
      .mockResolvedValue()

    const scope = effectScope()
    const chat = scope.run(() => usePipChat(ref(true)))
    expect(chat).toBeDefined()
    await chat!.ensureSession()

    await expect(chat!.endSession()).rejects.toThrow("PiP chat teardown failed")
    expect(deleteChatSession).toHaveBeenCalledTimes(1)

    await expect(chat!.ensureSession()).resolves.toEqual({ id: replacementId, generation: 1 })
    expect(
      vi.mocked(deleteChatSession).mock.calls.filter(([sessionId]) => sessionId === oldId),
    ).toHaveLength(2)

    scope.stop()
    await chat!.endSession()
  })

  it("retries retained TTS teardown debt before reopening without re-deleting the old session", async () => {
    const oldId = "019d0000-0000-7000-8000-000000000013"
    const replacementId = "019d0000-0000-7000-8000-000000000014"
    vi.spyOn(globalThis.crypto, "randomUUID")
      .mockReturnValueOnce(oldId)
      .mockReturnValueOnce(replacementId)
    vi.mocked(createChatSession).mockResolvedValue()
    ttsMocks.release
      .mockRejectedValueOnce(new Error("audio context close failed"))
      .mockResolvedValue()

    const scope = effectScope()
    const chat = scope.run(() => usePipChat(ref(false)))
    expect(chat).toBeDefined()
    await chat!.ensureSession()
    await vi.waitFor(() => expect(ttsMocks.speak).toHaveBeenCalledOnce())

    await expect(chat!.endSession()).rejects.toThrow("PiP chat teardown failed")
    await expect(chat!.ensureSession()).resolves.toEqual({ id: replacementId, generation: 1 })

    expect(ttsMocks.release).toHaveBeenCalledTimes(2)
    expect(
      vi.mocked(deleteChatSession).mock.calls.filter(([sessionId]) => sessionId === oldId),
    ).toHaveLength(1)

    scope.stop()
    await chat!.endSession()
  })

  it("permanently rejects session, send, and activity entry points after scope disposal", async () => {
    vi.useFakeTimers()
    const onActivity = vi.fn()
    const scope = effectScope()
    const chat = scope.run(() => usePipChat(ref(true), onActivity))
    expect(chat).toBeDefined()

    scope.stop()
    await chat!.endSession()
    await expect(chat!.ensureSession()).resolves.toBeNull()

    chat!.userInput.value = "late message"
    await chat!.sendMessage()
    chat!.handleKeydown(new KeyboardEvent("keydown", { key: "Enter" }))

    expect(createChatSession).not.toHaveBeenCalled()
    expect(sendChatMessageStream).not.toHaveBeenCalled()
    expect(deleteChatSession).not.toHaveBeenCalled()
    expect(onActivity).not.toHaveBeenCalled()
    expect(chat!.isSending.value).toBe(false)
    expect(chat!.messages.value).toHaveLength(0)
    expect(vi.getTimerCount()).toBe(0)
  })
})
