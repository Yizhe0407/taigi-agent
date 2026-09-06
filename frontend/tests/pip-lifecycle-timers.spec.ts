import { effectScope } from "vue"
import { afterEach, describe, expect, it, vi } from "vitest"

import { usePipEndConfirm } from "@/features/agent-chat/composables/usePipEndConfirm"
import { usePipIdleTimer } from "@/features/agent-chat/composables/usePipIdleTimer"
import { usePipMessageStore } from "@/features/agent-chat/composables/usePipMessageStore"
import { useThinkingFuse } from "@/features/agent-chat/composables/usePipTurnFuses"
import { usePipVoiceReveal } from "@/features/agent-chat/composables/usePipVoiceReveal"
import {
  consumeRetainedSynchronousTeardownFailure,
  settleRetainedSynchronousTeardowns,
} from "@/lib/component-lifecycle"

type ScheduledTimer = {
  callback: () => void
  delay: number | undefined
}

function installTimerHarness() {
  let nextId = 1
  const scheduled = new Map<number, ScheduledTimer>()
  const setTimeout = vi.spyOn(window, "setTimeout").mockImplementation(
    ((callback: TimerHandler, delay?: number) => {
      const id = nextId++
      scheduled.set(id, { callback: callback as () => void, delay })
      return id
    }) as typeof window.setTimeout,
  )
  const clearTimeout = vi.spyOn(globalThis, "clearTimeout").mockImplementation(
    ((id: number | undefined) => {
      if (id !== undefined) scheduled.delete(id)
    }) as typeof globalThis.clearTimeout,
  )

  return { scheduled, setTimeout, clearTimeout }
}

describe("PiP timer lifecycle ownership", () => {
  afterEach(() => {
    vi.useRealTimers()
    vi.restoreAllMocks()
    settleRetainedSynchronousTeardowns()
  })

  it("disposes the idle timeout and warning interval through one teardown", async () => {
    vi.useFakeTimers()
    const expire = vi.fn()
    const scope = effectScope()
    const idle = scope.run(() => usePipIdleTimer(expire))
    expect(idle).toBeDefined()

    idle!.resetIdleTimer()
    expect(vi.getTimerCount()).toBe(1)
    await vi.advanceTimersByTimeAsync(45_000)
    expect(idle!.showIdleWarning.value).toBe(true)
    expect(vi.getTimerCount()).toBe(1)

    scope.stop()
    expect(idle!.showIdleWarning.value).toBe(false)
    expect(vi.getTimerCount()).toBe(0)

    idle!.resetIdleTimer()
    idle!.markActivity()
    expect(vi.getTimerCount()).toBe(0)
    await vi.advanceTimersByTimeAsync(15_000)
    expect(expire).not.toHaveBeenCalled()
  })

  it("dismisses visible and pending end confirmation state before closing or disposal", () => {
    vi.useFakeTimers()
    const close = vi.fn()
    const beforeShow = vi.fn()
    const onContinue = vi.fn()
    const scope = effectScope()
    const confirmation = scope.run(() => usePipEndConfirm({
      isListening: () => true,
      close,
      beforeShow,
      onContinue,
    }))
    expect(confirmation).toBeDefined()

    confirmation!.onEndConversationEvent()
    expect(confirmation!.showEndConfirm.value).toBe(true)
    expect(vi.getTimerCount()).toBe(1)

    confirmation!.confirmEndNow()
    expect(close).toHaveBeenCalledOnce()
    expect(confirmation!.showEndConfirm.value).toBe(false)
    expect(confirmation!.resolvePending()).toBe(false)
    expect(vi.getTimerCount()).toBe(0)

    scope.stop()
    expect(vi.getTimerCount()).toBe(0)

    confirmation!.onEndConversationEvent()
    confirmation!.confirmEndNow()
    confirmation!.continueConversation()
    expect(confirmation!.resolvePending()).toBe(false)
    expect(beforeShow).toHaveBeenCalledOnce()
    expect(close).toHaveBeenCalledOnce()
    expect(onContinue).not.toHaveBeenCalled()
    expect(vi.getTimerCount()).toBe(0)
  })

  it("keeps watchdog and reveal ownership mutually exclusive under resetVoiceReply", async () => {
    vi.useFakeTimers()
    const watchdogExpire = vi.fn()
    const scope = effectScope()
    const state = scope.run(() => {
      const store = usePipMessageStore()
      const reveal = usePipVoiceReveal({
        messages: store.writer,
        displayedAgentText: { value: "" },
        onWatchdogExpire: watchdogExpire,
      })
      return { store, reveal }
    })
    expect(state).toBeDefined()

    state!.reveal.beginVoiceSession()
    expect(state!.reveal.receiveReply("fallback")).toBe(true)
    expect(vi.getTimerCount()).toBe(1)
    state!.reveal.revealSegment("abc", 300)
    expect(vi.getTimerCount()).toBe(1)

    state!.reveal.resetVoiceReply()
    expect(vi.getTimerCount()).toBe(0)
    await vi.advanceTimersByTimeAsync(4_000)
    expect(watchdogExpire).not.toHaveBeenCalled()
    expect(state!.store.messages.value.at(-1)?.text).toBe("a")

    scope.stop()
    expect(vi.getTimerCount()).toBe(0)
  })

  it("rolls back a user bubble when append mutates, re-enters, and throws", () => {
    const appendError = new Error("user append failed after mutation")
    let receiveReply: ((text: string) => boolean) | null = null
    const scope = effectScope()
    const state = scope.run(() => {
      const store = usePipMessageStore()
      const messages = {
        ...store.writer,
        append(message: Parameters<typeof store.writer.append>[0]) {
          store.writer.append(message)
          if (message.role === "user") {
            expect(receiveReply?.("reentrant reply")).toBe(false)
            throw appendError
          }
        },
      }
      const reveal = usePipVoiceReveal({
        messages,
        displayedAgentText: { value: "preserved" },
        onWatchdogExpire: vi.fn(),
      })
      receiveReply = reveal.receiveReply
      return { store, reveal }
    })
    expect(state).toBeDefined()

    expect(() => state!.reveal.beginVoiceTurn("question")).toThrow(appendError)
    expect(state!.store.messages.value).toEqual([])
    expect(state!.reveal.receiveReply("late reply")).toBe(false)
    expect(state!.reveal.revealSegment("late subtitle", 100)).toBe(false)

    scope.stop()
  })

  it("rolls back an assistant bubble when append synchronously resets its turn", () => {
    vi.useFakeTimers()
    let resetVoiceReply: (() => void) | null = null
    const displayedAgentText = { value: "preserved successor text" }
    const scope = effectScope()
    const state = scope.run(() => {
      const store = usePipMessageStore()
      const messages = {
        ...store.writer,
        append(message: Parameters<typeof store.writer.append>[0]) {
          store.writer.append(message)
          if (message.role === "assistant") resetVoiceReply?.()
        },
      }
      const reveal = usePipVoiceReveal({
        messages,
        displayedAgentText,
        onWatchdogExpire: vi.fn(),
      })
      resetVoiceReply = reveal.resetVoiceReply
      return { store, reveal }
    })
    expect(state).toBeDefined()

    expect(state!.reveal.beginVoiceSession()).toBe(true)
    expect(state!.reveal.revealSegment("subtitle", 800)).toBe(false)
    expect(state!.store.messages.value).toEqual([])
    expect(displayedAgentText.value).toBe("preserved successor text")
    expect(state!.reveal.receiveReply("late reply")).toBe(false)
    expect(vi.getTimerCount()).toBe(0)

    scope.stop()
  })

  it("stops a stale reveal step when appendText synchronously resets the turn", () => {
    vi.useFakeTimers()
    let resetVoiceReply: (() => void) | null = null
    const displayedAgentText = { value: "successor display" }
    const scope = effectScope()
    const state = scope.run(() => {
      const store = usePipMessageStore()
      const messages = {
        ...store.writer,
        appendText(id: string, text: string) {
          const updated = store.writer.appendText(id, text)
          resetVoiceReply?.()
          displayedAgentText.value = "successor display"
          return updated
        },
      }
      const reveal = usePipVoiceReveal({
        messages,
        displayedAgentText,
        onWatchdogExpire: vi.fn(),
      })
      resetVoiceReply = reveal.resetVoiceReply
      return { store, reveal }
    })
    expect(state).toBeDefined()

    expect(state!.reveal.beginVoiceSession()).toBe(true)
    expect(state!.reveal.revealSegment("ab", 100)).toBe(false)
    expect(state!.store.messages.value.at(-1)?.text).toBe("a")
    expect(displayedAgentText.value).toBe("successor display")
    expect(state!.reveal.receiveReply("late reply")).toBe(false)
    expect(vi.getTimerCount()).toBe(0)

    scope.stop()
  })

  it("joins a successor request that re-enters parked-reply publication", () => {
    let beginVoiceSession: (() => boolean) | null = null
    let reentrantBeginResult: boolean | null = null
    const displayedAgentText = { value: "" }
    const scope = effectScope()
    const state = scope.run(() => {
      const store = usePipMessageStore()
      const messages = {
        ...store.writer,
        setText(id: string, text: string) {
          const updated = store.writer.setText(id, text)
          reentrantBeginResult = beginVoiceSession?.() ?? null
          return updated
        },
      }
      const reveal = usePipVoiceReveal({
        messages,
        displayedAgentText,
        onWatchdogExpire: vi.fn(),
      })
      beginVoiceSession = reveal.beginVoiceSession
      return { store, reveal }
    })
    expect(state).toBeDefined()

    expect(state!.reveal.beginVoiceTurn("question")).toBe(true)
    expect(state!.reveal.markAudioStarted()).toBe(true)
    expect(state!.reveal.receiveReply("authoritative reply")).toBe(true)
    expect(state!.reveal.finishVoiceReply()).toBe(true)
    expect(reentrantBeginResult).toBe(false)
    expect(state!.store.messages.value.at(-1)?.text).toBe("authoritative reply")
    expect(displayedAgentText.value).toBe("authoritative reply")

    expect(state!.reveal.beginVoiceSession()).toBe(true)
    expect(displayedAgentText.value).toBe("authoritative reply")
    scope.stop()
  })

  it("joins clearTimeout re-entry before allowing a successor turn", () => {
    const timers = installTimerHarness()
    let beginVoiceSession: (() => boolean) | null = null
    let receiveReply: ((text: string) => boolean) | null = null
    const reentrantResults: boolean[] = []
    const normalClear = timers.clearTimeout.getMockImplementation()!
    timers.clearTimeout.mockImplementationOnce(((id?: number) => {
      normalClear(id)
      reentrantResults.push(beginVoiceSession?.() ?? true)
      reentrantResults.push(receiveReply?.("successor reply") ?? true)
    }) as typeof globalThis.clearTimeout)

    const scope = effectScope()
    const state = scope.run(() => {
      const store = usePipMessageStore()
      const reveal = usePipVoiceReveal({
        messages: store.writer,
        displayedAgentText: { value: "" },
        onWatchdogExpire: vi.fn(),
      })
      beginVoiceSession = reveal.beginVoiceSession
      receiveReply = reveal.receiveReply
      return { store, reveal }
    })
    expect(state).toBeDefined()

    expect(state!.reveal.beginVoiceSession()).toBe(true)
    expect(state!.reveal.receiveReply("old reply")).toBe(true)
    expect(state!.reveal.markAudioStarted()).toBe(false)
    expect(reentrantResults).toEqual([false, false])
    expect(timers.clearTimeout.mock.calls.map(([id]) => id)).toEqual([1])
    expect(timers.scheduled.size).toBe(0)

    expect(state!.reveal.beginVoiceSession()).toBe(true)
    expect(state!.reveal.receiveReply("new reply")).toBe(true)
    expect([...timers.scheduled.keys()]).toEqual([2])
    scope.stop()
  })

  it("retains an exact message rollback after remove mutates and throws", () => {
    const rollbackError = new Error("message remove failed after mutation")
    let resetVoiceReply: (() => void) | null = null
    let failRollback = true
    let removeCalls = 0
    const scope = effectScope()
    const state = scope.run(() => {
      const store = usePipMessageStore()
      const messages = {
        ...store.writer,
        append(message: Parameters<typeof store.writer.append>[0]) {
          store.writer.append(message)
          if (message.role === "assistant") resetVoiceReply?.()
        },
        remove(id: string) {
          removeCalls++
          const removed = store.writer.remove(id)
          if (failRollback) {
            failRollback = false
            throw rollbackError
          }
          return removed
        },
      }
      const reveal = usePipVoiceReveal({
        messages,
        displayedAgentText: { value: "" },
        onWatchdogExpire: vi.fn(),
      })
      resetVoiceReply = reveal.resetVoiceReply
      return { store, reveal }
    })
    expect(state).toBeDefined()

    expect(state!.reveal.beginVoiceSession()).toBe(true)
    expect(() => state!.reveal.revealSegment("subtitle", 800)).toThrow(
      "Failed to roll back PiP message voice-reply-1 after disposing",
    )
    expect(state!.store.messages.value).toEqual([])
    expect(removeCalls).toBe(1)

    expect(state!.reveal.beginVoiceSession()).toBe(true)
    expect(removeCalls).toBe(2)
    expect(state!.reveal.receiveReply("new reply")).toBe(true)
    scope.stop()
  })

  it("does not let stale silence cancel the current reply watchdog", async () => {
    vi.useFakeTimers()
    const watchdogExpire = vi.fn()
    const scope = effectScope()
    const state = scope.run(() => {
      const store = usePipMessageStore()
      const reveal = usePipVoiceReveal({
        messages: store.writer,
        displayedAgentText: { value: "" },
        onWatchdogExpire: watchdogExpire,
      })
      return { store, reveal }
    })
    expect(state).toBeDefined()

    state!.reveal.beginVoiceSession()
    expect(state!.reveal.receiveReply("fallback")).toBe(true)
    expect(state!.reveal.finishVoiceReply()).toBe(false)
    expect(vi.getTimerCount()).toBe(1)

    await vi.advanceTimersByTimeAsync(4_000)
    expect(watchdogExpire).toHaveBeenCalledOnce()
    expect(state!.store.messages.value.at(-1)?.text).toBe("fallback")

    scope.stop()
    expect(vi.getTimerCount()).toBe(0)
  })

  it("joins audio-before-reply ordering into one idempotent finish transition", () => {
    vi.useFakeTimers()
    const scope = effectScope()
    const state = scope.run(() => {
      const store = usePipMessageStore()
      const reveal = usePipVoiceReveal({
        messages: store.writer,
        displayedAgentText: { value: "" },
        onWatchdogExpire: vi.fn(),
      })
      return { store, reveal }
    })
    expect(state).toBeDefined()

    state!.reveal.beginVoiceSession()
    expect(state!.reveal.markAudioStarted()).toBe(true)
    expect(state!.reveal.receiveReply("full reply")).toBe(true)
    expect(vi.getTimerCount()).toBe(0)
    expect(state!.reveal.finishVoiceReply()).toBe(true)
    expect(state!.reveal.finishVoiceReply()).toBe(false)
    expect(state!.store.messages.value.at(-1)?.text).toBe("full reply")

    scope.stop()
  })

  it("makes watchdog expiry terminal so late voice events cannot resurrect the turn", async () => {
    vi.useFakeTimers()
    const watchdogExpire = vi.fn()
    const displayedAgentText = { value: "" }
    const scope = effectScope()
    const state = scope.run(() => {
      const store = usePipMessageStore()
      const reveal = usePipVoiceReveal({
        messages: store.writer,
        displayedAgentText,
        onWatchdogExpire: watchdogExpire,
      })
      return { store, reveal }
    })
    expect(state).toBeDefined()

    state!.reveal.beginVoiceSession()
    expect(state!.reveal.receiveReply("fallback")).toBe(true)
    await vi.advanceTimersByTimeAsync(4_000)

    expect(watchdogExpire).toHaveBeenCalledOnce()
    expect(state!.store.messages.value.at(-1)?.text).toBe("fallback")
    expect(displayedAgentText.value).toBe("fallback")
    expect(vi.getTimerCount()).toBe(0)

    expect(state!.reveal.receiveReply("late reply")).toBe(false)
    expect(state!.reveal.revealSegment("late subtitle", 1_000)).toBe(false)
    expect(state!.reveal.markAudioStarted()).toBe(false)
    expect(state!.store.messages.value.at(-1)?.text).toBe("fallback")
    expect(displayedAgentText.value).toBe("fallback")
    expect(vi.getTimerCount()).toBe(0)

    scope.stop()
  })

  it("makes normal finish terminal for late subtitle and bot-speaking events", () => {
    vi.useFakeTimers()
    const displayedAgentText = { value: "" }
    const scope = effectScope()
    const state = scope.run(() => {
      const store = usePipMessageStore()
      const reveal = usePipVoiceReveal({
        messages: store.writer,
        displayedAgentText,
        onWatchdogExpire: vi.fn(),
      })
      return { store, reveal }
    })
    expect(state).toBeDefined()

    state!.reveal.beginVoiceTurn("question")
    expect(state!.reveal.receiveReply("complete answer")).toBe(true)
    expect(state!.reveal.revealSegment("spoken", 600)).toBe(true)
    expect(state!.reveal.finishVoiceReply()).toBe(true)
    expect(state!.store.messages.value.at(-1)?.text).toBe("complete answer")
    expect(displayedAgentText.value).toBe("complete answer")
    expect(vi.getTimerCount()).toBe(0)

    expect(state!.reveal.revealSegment("late", 600)).toBe(false)
    expect(state!.reveal.markAudioStarted()).toBe(false)
    expect(state!.reveal.finishVoiceReply()).toBe(false)
    expect(state!.store.messages.value.at(-1)?.text).toBe("complete answer")
    expect(displayedAgentText.value).toBe("complete answer")
    expect(vi.getTimerCount()).toBe(0)

    scope.stop()
  })

  it("keeps reset and scope disposal terminal against late events", () => {
    vi.useFakeTimers()
    const displayedAgentText = { value: "" }
    const scope = effectScope()
    const state = scope.run(() => {
      const store = usePipMessageStore()
      const reveal = usePipVoiceReveal({
        messages: store.writer,
        displayedAgentText,
        onWatchdogExpire: vi.fn(),
      })
      return { store, reveal }
    })
    expect(state).toBeDefined()

    state!.reveal.beginVoiceSession()
    state!.reveal.resetVoiceReply()
    expect(state!.reveal.receiveReply("after reset")).toBe(false)
    expect(state!.reveal.revealSegment("after reset", 600)).toBe(false)

    scope.stop()
    expect(state!.reveal.beginVoiceSession()).toBe(false)
    expect(state!.reveal.beginVoiceTurn("after dispose")).toBe(false)
    expect(state!.reveal.receiveReply("after dispose")).toBe(false)
    expect(state!.reveal.markAudioStarted()).toBe(false)
    expect(state!.store.messages.value).toHaveLength(0)
    expect(displayedAgentText.value).toBe("")
    expect(vi.getTimerCount()).toBe(0)
  })

  it("does not let a disposed fuse re-arm its timeout", async () => {
    vi.useFakeTimers()
    const expire = vi.fn()
    const scope = effectScope()
    const fuse = scope.run(() => useThinkingFuse(expire))
    expect(fuse).toBeDefined()

    fuse!.start()
    expect(vi.getTimerCount()).toBe(1)
    scope.stop()
    expect(vi.getTimerCount()).toBe(0)

    fuse!.start()
    expect(vi.getTimerCount()).toBe(0)
    await vi.advanceTimersByTimeAsync(30_000)
    expect(expire).not.toHaveBeenCalled()
  })

  it("retains a fuse timeout after clear mutates then throws and blocks replacement", () => {
    const timers = installTimerHarness()
    const cleanupError = new Error("fuse clear failed after clearing")
    const normalClear = timers.clearTimeout.getMockImplementation()!
    timers.clearTimeout.mockImplementationOnce(((id?: number) => {
      normalClear(id)
      throw cleanupError
    }) as typeof globalThis.clearTimeout)
    const scope = effectScope()
    const fuse = scope.run(() => useThinkingFuse(vi.fn()))
    expect(fuse).toBeDefined()

    fuse!.start()
    expect([...timers.scheduled.keys()]).toEqual([1])

    expect(() => fuse!.start()).toThrow(cleanupError)
    expect(timers.setTimeout).toHaveBeenCalledOnce()
    expect(timers.clearTimeout.mock.calls.map(([id]) => id)).toEqual([1])

    expect(() => fuse!.clear()).not.toThrow()
    expect(timers.clearTimeout.mock.calls.map(([id]) => id)).toEqual([1, 1])
    expect(timers.scheduled.size).toBe(0)

    fuse!.start()
    expect([...timers.scheduled.keys()]).toEqual([2])
    scope.stop()
    expect(timers.clearTimeout.mock.calls.map(([id]) => id)).toEqual([1, 1, 2])
  })

  it("retains idle teardown debt and permanently gates its cached callback", () => {
    const timers = installTimerHarness()
    const cleanupError = new Error("idle clear failed after clearing")
    const normalClear = timers.clearTimeout.getMockImplementation()!
    timers.clearTimeout.mockImplementationOnce(((id?: number) => {
      normalClear(id)
      throw cleanupError
    }) as typeof globalThis.clearTimeout)
    const expire = vi.fn()
    const scope = effectScope()
    const idle = scope.run(() => usePipIdleTimer(expire))
    expect(idle).toBeDefined()

    idle!.resetIdleTimer()
    const cachedCallback = timers.scheduled.get(1)!.callback
    expect(() => scope.stop()).not.toThrow()
    expect(timers.clearTimeout.mock.calls.map(([id]) => id)).toEqual([1])
    expect(consumeRetainedSynchronousTeardownFailure()).toMatchObject({
      cause: cleanupError,
    })

    cachedCallback()
    expect(expire).not.toHaveBeenCalled()
    expect(timers.setTimeout).toHaveBeenCalledOnce()

    expect(() => settleRetainedSynchronousTeardowns()).not.toThrow()
    expect(consumeRetainedSynchronousTeardownFailure()).toBeNull()
    expect(timers.clearTimeout.mock.calls.map(([id]) => id)).toEqual([1, 1])
    idle!.resetIdleTimer()
    expect(timers.setTimeout).toHaveBeenCalledOnce()
  })

  it("does not retry a failed end-confirm interval inside nested scope disposal", () => {
    const timers = installTimerHarness()
    const cleanupError = new Error("countdown clear failed after clearing")
    const normalClear = timers.clearTimeout.getMockImplementation()!
    timers.clearTimeout.mockImplementationOnce(((id?: number) => {
      normalClear(id)
      throw cleanupError
    }) as typeof globalThis.clearTimeout)
    const scope = effectScope()
    const close = vi.fn(() => scope.stop())
    const confirmation = scope.run(() => usePipEndConfirm({
      isListening: () => true,
      close,
      beforeShow: vi.fn(),
      onContinue: vi.fn(),
    }))
    expect(confirmation).toBeDefined()

    confirmation!.onEndConversationEvent()
    expect([...timers.scheduled.keys()]).toEqual([1])

    expect(() => confirmation!.confirmEndNow()).toThrow(
      "Failed to confirm PiP conversation end",
    )
    expect(close).toHaveBeenCalledOnce()
    expect(confirmation!.showEndConfirm.value).toBe(false)
    expect(timers.clearTimeout.mock.calls.map(([id]) => id)).toEqual([1])

    expect(() => settleRetainedSynchronousTeardowns()).not.toThrow()
    expect(timers.clearTimeout.mock.calls.map(([id]) => id)).toEqual([1, 1])
  })

  it("retains a failed subtitle timer until the next voice-turn operation", () => {
    const timers = installTimerHarness()
    const cleanupError = new Error("subtitle clear failed after clearing")
    const normalClear = timers.clearTimeout.getMockImplementation()!
    timers.clearTimeout.mockImplementationOnce(((id?: number) => {
      normalClear(id)
      throw cleanupError
    }) as typeof globalThis.clearTimeout)
    const displayedAgentText = { value: "" }
    const scope = effectScope()
    const state = scope.run(() => {
      const store = usePipMessageStore()
      const reveal = usePipVoiceReveal({
        messages: store.writer,
        displayedAgentText,
        onWatchdogExpire: vi.fn(),
      })
      return { store, reveal }
    })
    expect(state).toBeDefined()

    expect(state!.reveal.beginVoiceSession()).toBe(true)
    expect(state!.reveal.revealSegment("ab", 100)).toBe(true)
    const cachedCallback = timers.scheduled.get(1)!.callback
    expect(displayedAgentText.value).toBe("a")

    expect(() => state!.reveal.resetVoiceReply()).toThrow(cleanupError)
    expect(timers.clearTimeout.mock.calls.map(([id]) => id)).toEqual([1])
    cachedCallback()
    expect(displayedAgentText.value).toBe("a")
    expect(timers.setTimeout).toHaveBeenCalledOnce()

    expect(state!.reveal.beginVoiceSession()).toBe(true)
    expect(timers.clearTimeout.mock.calls.map(([id]) => id)).toEqual([1, 1])
    expect(state!.reveal.receiveReply("fallback")).toBe(true)
    expect([...timers.scheduled.keys()]).toEqual([2])

    scope.stop()
    expect(timers.clearTimeout.mock.calls.map(([id]) => id)).toEqual([1, 1, 2])
  })

})
