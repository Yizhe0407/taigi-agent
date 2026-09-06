import { effectScope } from "vue"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { synthesizeSpeech } from "@/features/agent-chat/api/chat"
import { useTts } from "@/features/agent-chat/composables/useTts"
import { reportClientEvent } from "@/lib/report-client-event"

vi.mock("@/features/agent-chat/api/chat", () => ({
  synthesizeSpeech: vi.fn(),
}))
vi.mock("@/lib/report-client-event", () => ({
  reportClientEvent: vi.fn(),
}))

class FakeAudio {
  static instances: FakeAudio[] = []

  duration = Number.NaN
  onended: (() => void) | null = null
  onerror: (() => void) | null = null
  onloadedmetadata: (() => void) | null = null
  srcObject: MediaProvider | null = null
  readonly pause = vi.fn()
  readonly load = vi.fn()
  readonly play = vi.fn(async () => {})
  readonly removeAttribute = vi.fn()

  constructor(readonly src: string) {
    FakeAudio.instances.push(this)
  }
}

async function flushOwnedOperations() {
  for (let index = 0; index < 12; index += 1) await Promise.resolve()
}

async function expectPending(operation: Promise<unknown>) {
  let settled = false
  void operation.then(
    () => { settled = true },
    () => { settled = true },
  )
  await flushOwnedOperations()
  expect(settled).toBe(false)
}

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}

function nestedErrorMessages(error: unknown): string[] {
  const messages: string[] = []
  const visited = new Set<unknown>()
  const visit = (failure: unknown) => {
    if (visited.has(failure)) return
    visited.add(failure)
    if (failure instanceof Error) messages.push(failure.message)
    if (failure instanceof AggregateError) {
      for (const nested of failure.errors) visit(nested)
    }
    if (failure instanceof Error && failure.cause !== undefined) {
      visit(failure.cause)
    }
  }
  visit(error)
  return messages
}

function abortableTtsRequest(signal?: AbortSignal): Promise<Blob> {
  return new Promise((_resolve, reject) => {
    if (!signal) return
    const onAbort = () => reject(signal.reason)
    signal.addEventListener("abort", onAbort, { once: true })
    if (signal.aborted) onAbort()
  })
}

describe("useTts", () => {
  beforeEach(() => {
    vi.useFakeTimers()
    FakeAudio.instances = []
    vi.mocked(reportClientEvent).mockClear()
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
    vi.mocked(synthesizeSpeech).mockReset()
  })

  it("releases Audio and blob URL when metadata never arrives", async () => {
    const closeContext = vi.fn(async () => {})
    class FakeAudioContext {
      state = "running"
      close = closeContext
    }

    const revokeObjectURL = vi.fn()
    vi.stubGlobal("Audio", FakeAudio)
    vi.stubGlobal("AudioContext", FakeAudioContext)
    vi.stubGlobal("URL", {
      createObjectURL: vi.fn(() => "blob:tts-turn"),
      revokeObjectURL,
    })
    vi.mocked(synthesizeSpeech).mockResolvedValue(new Blob(["audio"]))

    const scope = effectScope()
    const tts = scope.run(() => useTts())
    expect(tts).toBeDefined()

    const speaking = tts!.speak("你好")
    await vi.advanceTimersByTimeAsync(0)
    expect(FakeAudio.instances).toHaveLength(1)

    await vi.advanceTimersByTimeAsync(3_000)
    await expect(speaking).resolves.toBeNull()

    const audio = FakeAudio.instances[0]!
    expect(audio.pause).toHaveBeenCalledOnce()
    expect(audio.removeAttribute).toHaveBeenCalledWith("src")
    expect(audio.load).toHaveBeenCalledOnce()
    expect(revokeObjectURL).toHaveBeenCalledOnce()
    expect(tts!.ttsState.value).toBe("idle")

    scope.stop()
    await flushOwnedOperations()
    expect(closeContext).toHaveBeenCalledOnce()
  })

  it("reports a non-cancellation request failure and keeps cancel private", async () => {
    const closeContext = vi.fn(async () => {})
    class FakeAudioContext {
      state = "running"
      close = closeContext
    }

    vi.stubGlobal("AudioContext", FakeAudioContext)
    vi.mocked(synthesizeSpeech).mockRejectedValue(new Error("TTS upstream failed"))

    const scope = effectScope()
    const tts = scope.run(() => useTts())
    expect(tts).toBeDefined()
    expect("cancel" in tts!).toBe(false)

    await expect(tts!.speak("你好")).resolves.toBeNull()
    expect(reportClientEvent).toHaveBeenCalledOnce()
    expect(reportClientEvent).toHaveBeenCalledWith("tts_request_error", "TTS upstream failed")

    scope.stop()
    await flushOwnedOperations()
    expect(closeContext).toHaveBeenCalledOnce()
  })

  it("reports the owned request deadline instead of silently hanging", async () => {
    const closeContext = vi.fn(async () => {})
    class FakeAudioContext {
      state = "running"
      close = closeContext
    }

    vi.stubGlobal("AudioContext", FakeAudioContext)
    vi.mocked(synthesizeSpeech).mockImplementation((_text, signal) => abortableTtsRequest(signal))

    const scope = effectScope()
    const tts = scope.run(() => useTts())
    expect(tts).toBeDefined()

    const speaking = tts!.speak("你好")
    await vi.advanceTimersByTimeAsync(50_000)
    await expect(speaking).resolves.toBeNull()
    expect(reportClientEvent).toHaveBeenCalledWith(
      "tts_request_timeout",
      "TTS request timed out",
    )

    scope.stop()
    await flushOwnedOperations()
    expect(closeContext).toHaveBeenCalledOnce()
  })

  it("waits for the owned AudioContext close before creating a replacement", async () => {
    const firstClose = deferred<void>()
    const contexts: FakeAudioContext[] = []
    class FakeAudioContext {
      state = "running"
      readonly close = vi.fn(() => {
        this.state = "closed"
        return contexts.length === 1 ? firstClose.promise : Promise.resolve()
      })

      constructor() {
        contexts.push(this)
      }
    }

    vi.stubGlobal("AudioContext", FakeAudioContext)
    vi.mocked(synthesizeSpeech).mockImplementation((_text, signal) => abortableTtsRequest(signal))

    const scope = effectScope()
    const tts = scope.run(() => useTts())
    expect(tts).toBeDefined()

    const first = tts!.speak("第一句")
    await vi.advanceTimersByTimeAsync(0)
    expect(contexts).toHaveLength(1)
    expect(synthesizeSpeech).toHaveBeenCalledOnce()

    const firstRelease = tts!.release()
    const second = tts!.speak("第二句")
    await vi.advanceTimersByTimeAsync(0)
    expect(contexts).toHaveLength(1)
    expect(synthesizeSpeech).toHaveBeenCalledOnce()

    firstClose.resolve()
    await expect(firstRelease).resolves.toBeUndefined()
    await vi.waitFor(() => {
      expect(contexts).toHaveLength(2)
      expect(synthesizeSpeech).toHaveBeenCalledTimes(2)
    })

    await expect(tts!.release()).resolves.toBeUndefined()
    scope.stop()
    await flushOwnedOperations()
    await expect(Promise.all([first, second])).resolves.toEqual([null, null])
    expect(contexts[0]!.close).toHaveBeenCalledOnce()
    expect(contexts[1]!.close).toHaveBeenCalledOnce()
  })

  it("releases superseded turn resources exactly once", async () => {
    const closeContext = vi.fn(async () => {})
    class FakeAudioContext {
      state = "running"
      close = closeContext
    }

    const createObjectURL = vi.fn()
      .mockReturnValueOnce("blob:first-turn")
      .mockReturnValueOnce("blob:second-turn")
    const revokeObjectURL = vi.fn()
    vi.stubGlobal("Audio", FakeAudio)
    vi.stubGlobal("AudioContext", FakeAudioContext)
    vi.stubGlobal("URL", { createObjectURL, revokeObjectURL })
    vi.mocked(synthesizeSpeech).mockResolvedValue(new Blob(["audio"]))

    const scope = effectScope()
    const tts = scope.run(() => useTts())
    expect(tts).toBeDefined()

    const first = tts!.speak("第一句")
    await vi.advanceTimersByTimeAsync(0)
    expect(FakeAudio.instances).toHaveLength(1)

    const second = tts!.speak("第二句")
    await vi.advanceTimersByTimeAsync(0)
    expect(FakeAudio.instances).toHaveLength(2)

    const [firstAudio, secondAudio] = FakeAudio.instances
    expect(firstAudio!.pause).toHaveBeenCalledOnce()
    expect(firstAudio!.removeAttribute).toHaveBeenCalledOnce()
    expect(firstAudio!.load).toHaveBeenCalledOnce()

    await expect(tts!.release()).resolves.toBeUndefined()
    scope.stop()
    await flushOwnedOperations()
    await expect(Promise.all([first, second])).resolves.toEqual([null, null])

    for (const audio of [firstAudio!, secondAudio!]) {
      expect(audio.pause).toHaveBeenCalledOnce()
      expect(audio.removeAttribute).toHaveBeenCalledOnce()
      expect(audio.load).toHaveBeenCalledOnce()
    }
    expect(revokeObjectURL).toHaveBeenCalledTimes(2)
    expect(closeContext).toHaveBeenCalledOnce()
  })

  it("retains only failed turn cleanup steps and backpressures the next turn", async () => {
    const closeContext = vi.fn(async () => {})
    class FakeAudioContext {
      state = "running"
      close = closeContext
    }

    const revokeObjectURL = vi.fn()
    vi.stubGlobal("Audio", FakeAudio)
    vi.stubGlobal("AudioContext", FakeAudioContext)
    vi.stubGlobal("URL", {
      createObjectURL: vi.fn(() => `blob:tts-${FakeAudio.instances.length}`),
      revokeObjectURL,
    })
    vi.mocked(synthesizeSpeech).mockResolvedValue(new Blob(["audio"]))

    const scope = effectScope()
    const tts = scope.run(() => useTts())
    expect(tts).toBeDefined()

    const first = tts!.speak("第一句")
    await vi.advanceTimersByTimeAsync(0)
    const firstAudio = FakeAudio.instances[0]!
    firstAudio.pause.mockImplementationOnce(() => {
      throw new Error("pause failed")
    })

    const blocked = tts!.speak("被阻擋的第二句")
    const [blockedResult, firstResult] = await Promise.allSettled([blocked, first])
    expect(blockedResult.status).toBe("rejected")
    expect(firstResult.status).toBe("rejected")
    if (blockedResult.status !== "rejected" || firstResult.status !== "rejected") return
    expect(blockedResult.reason).toBeInstanceOf(AggregateError)
    expect(blockedResult.reason.message).toBe("TTS teardown failed")
    expect(firstResult.reason).toBe(blockedResult.reason)
    expect(FakeAudio.instances).toHaveLength(1)
    expect(firstAudio.pause).toHaveBeenCalledOnce()
    expect(firstAudio.removeAttribute).toHaveBeenCalledOnce()
    expect(firstAudio.load).toHaveBeenCalledOnce()
    expect(revokeObjectURL).toHaveBeenCalledOnce()

    const third = tts!.speak("第三句")
    await vi.waitFor(() => expect(FakeAudio.instances).toHaveLength(2))
    expect(firstAudio.pause).toHaveBeenCalledTimes(2)
    expect(firstAudio.removeAttribute).toHaveBeenCalledOnce()
    expect(firstAudio.load).toHaveBeenCalledOnce()
    expect(revokeObjectURL).toHaveBeenCalledOnce()

    await expect(tts!.release()).resolves.toBeUndefined()
    await expect(third).resolves.toBeNull()
    scope.stop()
    await flushOwnedOperations()
  })

  it("retains a failed AudioContext close as debt and retries that exact context", async () => {
    const contexts: FakeAudioContext[] = []
    const closeContext = vi.fn()
      .mockRejectedValueOnce(new Error("close failed"))
      .mockResolvedValue(undefined)
    class FakeAudioContext {
      state = "running"
      close = closeContext

      constructor() {
        contexts.push(this)
      }
    }

    vi.stubGlobal("AudioContext", FakeAudioContext)
    vi.mocked(synthesizeSpeech).mockImplementation((_text, signal) => abortableTtsRequest(signal))

    const scope = effectScope()
    const tts = scope.run(() => useTts())
    expect(tts).toBeDefined()

    const first = tts!.speak("第一句")
    await vi.advanceTimersByTimeAsync(0)
    const releasing = tts!.release()
    const [releaseResult, firstResult] = await Promise.allSettled([releasing, first])
    expect(releaseResult.status).toBe("rejected")
    expect(firstResult.status).toBe("rejected")
    if (releaseResult.status !== "rejected" || firstResult.status !== "rejected") return
    expect(releaseResult.reason).toBeInstanceOf(AggregateError)
    expect(releaseResult.reason.message).toBe("TTS teardown failed")
    expect(firstResult.reason).toBe(releaseResult.reason)

    expect(contexts).toHaveLength(1)
    expect(closeContext).toHaveBeenCalledOnce()
    expect(reportClientEvent).toHaveBeenCalledWith(
      "tts_cleanup_error",
      "audio context: close failed",
    )

    const second = tts!.speak("第二句")
    await vi.waitFor(() => {
      expect(closeContext).toHaveBeenCalledTimes(2)
      expect(contexts).toHaveLength(2)
      expect(synthesizeSpeech).toHaveBeenCalledTimes(2)
    })

    await expect(tts!.release()).resolves.toBeUndefined()
    await expect(second).resolves.toBeNull()
    expect(closeContext).toHaveBeenCalledTimes(3)
    scope.stop()
    await flushOwnedOperations()
  })

  it("publishes the turn owner before queued work so scope disposal prevents all acquisition", async () => {
    const contexts: FakeAudioContext[] = []
    class FakeAudioContext {
      state = "running"
      close = vi.fn(async () => {})

      constructor() {
        contexts.push(this)
      }
    }

    vi.stubGlobal("AudioContext", FakeAudioContext)
    vi.mocked(synthesizeSpeech).mockImplementation((_text, signal) => abortableTtsRequest(signal))

    const scope = effectScope()
    const tts = scope.run(() => useTts())
    expect(tts).toBeDefined()

    const speaking = tts!.speak("尚未開始的句子")
    // The pending speak identity is published before queued work. No browser
    // resource is acquired until that identity survives the initial teardown.
    await Promise.resolve()
    await Promise.resolve()
    expect(vi.getTimerCount()).toBe(0)

    scope.stop()
    await flushOwnedOperations()
    await expect(speaking).resolves.toBeNull()

    expect(contexts).toHaveLength(0)
    expect(synthesizeSpeech).not.toHaveBeenCalled()
    expect(vi.getTimerCount()).toBe(0)
  })

  it("rolls back the exact request timer when scheduling synchronously re-enters release", async () => {
    const closeContext = vi.fn(async () => {})
    class FakeAudioContext {
      state = "running"
      close = closeContext
    }

    let tts: ReturnType<typeof useTts> | undefined
    let releasing: Promise<void> | null = null
    let cachedCallback: (() => void) | null = null
    vi.stubGlobal("AudioContext", FakeAudioContext)
    vi.spyOn(window, "setTimeout").mockImplementation(
      ((callback: TimerHandler) => {
        cachedCallback = callback as () => void
        releasing = tts!.release()
        return 91
      }) as typeof window.setTimeout,
    )
    const clearTimeout = vi.spyOn(globalThis, "clearTimeout")

    const scope = effectScope()
    tts = scope.run(() => useTts())
    expect(tts).toBeDefined()

    const speaking = tts!.speak("timer reentry")
    await flushOwnedOperations()
    await expect(releasing!).resolves.toBeUndefined()
    await expect(speaking).resolves.toBeNull()

    expect(clearTimeout).toHaveBeenCalledOnce()
    expect(clearTimeout).toHaveBeenCalledWith(91)
    expect(synthesizeSpeech).not.toHaveBeenCalled()
    ;(cachedCallback as (() => void) | null)?.()
    expect(reportClientEvent).not.toHaveBeenCalledWith(
      "tts_request_timeout",
      expect.anything(),
    )
    expect(closeContext).toHaveBeenCalledOnce()

    scope.stop()
    await flushOwnedOperations()
  })

  it("revokes a blob URL returned after createObjectURL synchronously re-enters release", async () => {
    const closeContext = vi.fn(async () => {})
    class FakeAudioContext {
      state = "running"
      close = closeContext
    }

    let tts: ReturnType<typeof useTts> | undefined
    let releasing: Promise<void> | null = null
    const revokeObjectURL = vi.fn()
    vi.stubGlobal("Audio", FakeAudio)
    vi.stubGlobal("AudioContext", FakeAudioContext)
    vi.stubGlobal("URL", {
      createObjectURL: vi.fn(() => {
        releasing = tts!.release()
        return "blob:late-url"
      }),
      revokeObjectURL,
    })
    vi.mocked(synthesizeSpeech).mockResolvedValue(new Blob(["audio"]))

    const scope = effectScope()
    tts = scope.run(() => useTts())
    expect(tts).toBeDefined()

    const speaking = tts!.speak("late URL")
    await flushOwnedOperations()
    await expect(releasing!).resolves.toBeUndefined()
    await expect(speaking).resolves.toBeNull()

    expect(revokeObjectURL).toHaveBeenCalledOnce()
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:late-url")
    expect(FakeAudio.instances).toHaveLength(0)
    expect(closeContext).toHaveBeenCalledOnce()

    scope.stop()
    await flushOwnedOperations()
  })

  it("cleans an Audio element returned after its constructor synchronously re-enters release", async () => {
    const closeContext = vi.fn(async () => {})
    class FakeAudioContext {
      state = "running"
      close = closeContext
    }

    let tts: ReturnType<typeof useTts> | undefined
    let releasing: Promise<void> | null = null
    class ReentrantAudio extends FakeAudio {
      constructor(src: string) {
        super(src)
        releasing = tts!.release()
      }
    }
    const revokeObjectURL = vi.fn()
    vi.stubGlobal("Audio", ReentrantAudio)
    vi.stubGlobal("AudioContext", FakeAudioContext)
    vi.stubGlobal("URL", {
      createObjectURL: vi.fn(() => "blob:late-audio"),
      revokeObjectURL,
    })
    vi.mocked(synthesizeSpeech).mockResolvedValue(new Blob(["audio"]))

    const scope = effectScope()
    tts = scope.run(() => useTts())
    expect(tts).toBeDefined()

    const speaking = tts!.speak("late Audio")
    await flushOwnedOperations()
    await expect(releasing!).resolves.toBeUndefined()
    await expect(speaking).resolves.toBeNull()

    const audio = FakeAudio.instances[0]!
    expect(audio.pause).toHaveBeenCalledOnce()
    expect(audio.removeAttribute).toHaveBeenCalledOnce()
    expect(audio.load).toHaveBeenCalledOnce()
    expect(revokeObjectURL).toHaveBeenCalledOnce()
    expect(closeContext).toHaveBeenCalledOnce()

    scope.stop()
    await flushOwnedOperations()
  })

  it("publishes a late AudioContext and joins its close before admitting a successor", async () => {
    const firstClose = deferred<void>()
    const contexts: FakeAudioContext[] = []
    let tts: ReturnType<typeof useTts> | undefined
    let reentrantRelease: Promise<void> | null = null
    class FakeAudioContext {
      state: AudioContextState = "running"
      readonly close = vi.fn(() => {
        this.state = "closed"
        return contexts[0] === this ? firstClose.promise : Promise.resolve()
      })

      constructor() {
        contexts.push(this)
        if (contexts.length === 1) reentrantRelease = tts!.release()
      }
    }

    vi.stubGlobal("AudioContext", FakeAudioContext)
    vi.mocked(synthesizeSpeech).mockImplementation((_text, signal) => abortableTtsRequest(signal))

    const scope = effectScope()
    tts = scope.run(() => useTts())
    expect(tts).toBeDefined()

    const first = tts!.speak("constructor reentry")
    await flushOwnedOperations()
    expect(contexts).toHaveLength(1)
    expect(contexts[0]!.close).toHaveBeenCalledOnce()
    expect(synthesizeSpeech).not.toHaveBeenCalled()
    await expectPending(reentrantRelease!)
    await expectPending(first)

    const second = tts!.speak("successor")
    await flushOwnedOperations()
    expect(contexts).toHaveLength(1)

    firstClose.resolve()
    await expect(reentrantRelease!).resolves.toBeUndefined()
    await expect(first).resolves.toBeNull()
    await vi.waitFor(() => {
      expect(contexts).toHaveLength(2)
      expect(synthesizeSpeech).toHaveBeenCalledOnce()
    })

    await expect(tts!.release()).resolves.toBeUndefined()
    await expect(second).resolves.toBeNull()
    expect(contexts[1]!.close).toHaveBeenCalledOnce()
    scope.stop()
    await flushOwnedOperations()
  })

  it("joins the physical AudioContext resume after logical cancellation", async () => {
    const resumeOperation = deferred<void>()
    const closeContext = vi.fn(async () => {})
    class FakeAudioContext {
      state: AudioContextState = "suspended"
      close = closeContext
      resume = vi.fn(() => resumeOperation.promise)
    }

    vi.stubGlobal("AudioContext", FakeAudioContext)
    const scope = effectScope()
    const tts = scope.run(() => useTts())
    expect(tts).toBeDefined()

    const speaking = tts!.speak("pending resume")
    await flushOwnedOperations()
    const releasing = tts!.release()
    await expectPending(releasing)
    await expectPending(speaking)
    expect(closeContext).toHaveBeenCalledOnce()
    expect(synthesizeSpeech).not.toHaveBeenCalled()

    resumeOperation.resolve()
    await expect(releasing).resolves.toBeUndefined()
    await expect(speaking).resolves.toBeNull()
    scope.stop()
    await flushOwnedOperations()
  })

  it.each(["onloadedmetadata", "onerror"] as const)(
    "rolls back the %s callback when its setter synchronously re-enters release",
    async (triggerProperty) => {
      const closeContext = vi.fn(async () => {})
      class FakeAudioContext {
        state = "running"
        close = closeContext
      }

      let tts: ReturnType<typeof useTts> | undefined
      let releasing: Promise<void> | null = null
      let triggered = false
      class ReentrantMetadataAudio extends FakeAudio {
        constructor(src: string) {
          super(src)
          let loaded: (() => void) | null = null
          let failed: (() => void) | null = null
          Object.defineProperty(this, "onloadedmetadata", {
            configurable: true,
            get: () => loaded,
            set: (handler: (() => void) | null) => {
              loaded = handler
              if (triggerProperty === "onloadedmetadata" && handler && !triggered) {
                triggered = true
                releasing = tts!.release()
              }
            },
          })
          Object.defineProperty(this, "onerror", {
            configurable: true,
            get: () => failed,
            set: (handler: (() => void) | null) => {
              failed = handler
              if (triggerProperty === "onerror" && handler && !triggered) {
                triggered = true
                releasing = tts!.release()
              }
            },
          })
        }
      }

      const revokeObjectURL = vi.fn()
      vi.stubGlobal("Audio", ReentrantMetadataAudio)
      vi.stubGlobal("AudioContext", FakeAudioContext)
      vi.stubGlobal("URL", {
        createObjectURL: vi.fn(() => `blob:${triggerProperty}`),
        revokeObjectURL,
      })
      vi.mocked(synthesizeSpeech).mockResolvedValue(new Blob(["audio"]))

      const scope = effectScope()
      tts = scope.run(() => useTts())
      expect(tts).toBeDefined()

      const speaking = tts!.speak(triggerProperty)
      await flushOwnedOperations()
      await expect(releasing!).resolves.toBeUndefined()
      await expect(speaking).resolves.toBeNull()

      const audio = FakeAudio.instances[0]!
      expect(audio.onloadedmetadata).toBeNull()
      expect(audio.onerror).toBeNull()
      expect(audio.pause).toHaveBeenCalledOnce()
      expect(audio.removeAttribute).toHaveBeenCalledOnce()
      expect(audio.load).toHaveBeenCalledOnce()
      expect(revokeObjectURL).toHaveBeenCalledOnce()
      expect(vi.getTimerCount()).toBe(0)
      expect(closeContext).toHaveBeenCalledOnce()

      scope.stop()
      await flushOwnedOperations()
    },
  )

  it("rolls back the metadata abort listener when registration synchronously re-enters release", async () => {
    const closeContext = vi.fn(async () => {})
    class FakeAudioContext {
      state = "running"
      close = closeContext
    }

    let tts: ReturnType<typeof useTts> | undefined
    let releasing: Promise<void> | null = null
    let abortRegistrations = 0
    let abortRemovals = 0
    const addEventListener = AbortSignal.prototype.addEventListener
    const removeEventListener = AbortSignal.prototype.removeEventListener
    vi.spyOn(AbortSignal.prototype, "addEventListener").mockImplementation(
      function (type, listener, options) {
        addEventListener.call(this, type, listener, options)
        if (type !== "abort") return
        abortRegistrations += 1
        if (abortRegistrations === 2) releasing = tts!.release()
      },
    )
    vi.spyOn(AbortSignal.prototype, "removeEventListener").mockImplementation(
      function (type, listener, options) {
        removeEventListener.call(this, type, listener, options)
        if (type === "abort") abortRemovals += 1
      },
    )

    vi.stubGlobal("Audio", FakeAudio)
    vi.stubGlobal("AudioContext", FakeAudioContext)
    vi.stubGlobal("URL", {
      createObjectURL: vi.fn(() => "blob:metadata-abort"),
      revokeObjectURL: vi.fn(),
    })
    vi.mocked(synthesizeSpeech).mockResolvedValue(new Blob(["audio"]))

    const scope = effectScope()
    tts = scope.run(() => useTts())
    expect(tts).toBeDefined()

    const speaking = tts!.speak("metadata abort listener")
    await flushOwnedOperations()
    await expect(releasing!).resolves.toBeUndefined()
    await expect(speaking).resolves.toBeNull()

    expect(abortRegistrations).toBe(2)
    expect(abortRemovals).toBe(2)
    expect(FakeAudio.instances[0]!.pause).toHaveBeenCalledOnce()
    expect(closeContext).toHaveBeenCalledOnce()
    scope.stop()
    await flushOwnedOperations()
  })

  it("rolls back the exact metadata deadline when scheduling synchronously re-enters release", async () => {
    const closeContext = vi.fn(async () => {})
    class FakeAudioContext {
      state = "running"
      close = closeContext
    }

    let tts: ReturnType<typeof useTts> | undefined
    let releasing: Promise<void> | null = null
    let schedules = 0
    let cachedMetadataCallback: (() => void) | null = null
    const nativeSetTimeout = window.setTimeout.bind(window)
    vi.spyOn(window, "setTimeout").mockImplementation(
      ((callback: TimerHandler, delay?: number) => {
        schedules += 1
        const id = nativeSetTimeout(callback, delay)
        if (schedules === 2) {
          cachedMetadataCallback = callback as () => void
          releasing = tts!.release()
        }
        return id
      }) as typeof window.setTimeout,
    )
    const clearTimeout = vi.spyOn(globalThis, "clearTimeout")

    vi.stubGlobal("Audio", FakeAudio)
    vi.stubGlobal("AudioContext", FakeAudioContext)
    vi.stubGlobal("URL", {
      createObjectURL: vi.fn(() => "blob:metadata-deadline"),
      revokeObjectURL: vi.fn(),
    })
    vi.mocked(synthesizeSpeech).mockResolvedValue(new Blob(["audio"]))

    const scope = effectScope()
    tts = scope.run(() => useTts())
    expect(tts).toBeDefined()

    const speaking = tts!.speak("metadata deadline")
    await flushOwnedOperations()
    await expect(releasing!).resolves.toBeUndefined()
    await expect(speaking).resolves.toBeNull()

    expect(schedules).toBe(2)
    expect(clearTimeout).toHaveBeenCalledTimes(2)
    ;(cachedMetadataCallback as (() => void) | null)?.()
    expect(reportClientEvent).not.toHaveBeenCalledWith(
      "tts_metadata_error",
      "TTS metadata timed out",
    )
    expect(FakeAudio.instances[0]!.pause).toHaveBeenCalledOnce()
    expect(closeContext).toHaveBeenCalledOnce()
    scope.stop()
    await flushOwnedOperations()
  })

  it("rolls back a playback callback whose setter synchronously finishes the turn", async () => {
    const disconnectSource = vi.fn()
    const disconnectAnalyser = vi.fn()
    const source = {
      connect: vi.fn(),
      disconnect: disconnectSource,
    }
    const analyser = {
      fftSize: 0,
      smoothingTimeConstant: 0,
      minDecibels: 0,
      maxDecibels: 0,
      frequencyBinCount: 32,
      connect: vi.fn(),
      disconnect: disconnectAnalyser,
    }
    const closeContext = vi.fn(async () => {})
    class FakeAudioContext {
      state = "running"
      sampleRate = 48_000
      destination = {}
      close = closeContext
      createMediaElementSource = vi.fn(() => source)
      createAnalyser = vi.fn(() => analyser)
    }

    vi.stubGlobal("Audio", FakeAudio)
    vi.stubGlobal("AudioContext", FakeAudioContext)
    vi.stubGlobal("URL", {
      createObjectURL: vi.fn(() => "blob:playback-callback"),
      revokeObjectURL: vi.fn(),
    })
    vi.mocked(synthesizeSpeech).mockResolvedValue(new Blob(["audio"]))

    const scope = effectScope()
    const tts = scope.run(() => useTts())
    expect(tts).toBeDefined()

    const speaking = tts!.speak("synchronous ended callback")
    await flushOwnedOperations()
    const audio = FakeAudio.instances[0]!
    let ended: (() => void) | null = null
    let fired = false
    Object.defineProperty(audio, "onended", {
      configurable: true,
      get: () => ended,
      set: (handler: (() => void) | null) => {
        ended = handler
        if (handler && !fired) {
          fired = true
          handler()
        }
      },
    })
    audio.duration = 1
    audio.onloadedmetadata?.()

    await expect(speaking).resolves.toBeNull()
    expect(audio.play).not.toHaveBeenCalled()
    expect(audio.onended).toBeNull()
    expect(disconnectSource).toHaveBeenCalledOnce()
    expect(disconnectAnalyser).toHaveBeenCalledOnce()
    expect(audio.pause).toHaveBeenCalledOnce()

    scope.stop()
    await flushOwnedOperations()
    expect(closeContext).toHaveBeenCalledOnce()
  })

  it("joins the physical audio.play operation after logical cancellation", async () => {
    const playOperation = deferred<void>()
    const source = {
      connect: vi.fn(),
      disconnect: vi.fn(),
    }
    const analyser = {
      fftSize: 0,
      smoothingTimeConstant: 0,
      minDecibels: 0,
      maxDecibels: 0,
      frequencyBinCount: 32,
      connect: vi.fn(),
      disconnect: vi.fn(),
    }
    const closeContext = vi.fn(async () => {})
    class FakeAudioContext {
      state = "running"
      sampleRate = 48_000
      destination = {}
      close = closeContext
      createMediaElementSource = vi.fn(() => source)
      createAnalyser = vi.fn(() => analyser)
    }

    vi.stubGlobal("Audio", FakeAudio)
    vi.stubGlobal("AudioContext", FakeAudioContext)
    vi.stubGlobal("URL", {
      createObjectURL: vi.fn(() => "blob:pending-play"),
      revokeObjectURL: vi.fn(),
    })
    vi.mocked(synthesizeSpeech).mockResolvedValue(new Blob(["audio"]))

    const scope = effectScope()
    const tts = scope.run(() => useTts())
    expect(tts).toBeDefined()

    const speaking = tts!.speak("pending play")
    await flushOwnedOperations()
    const audio = FakeAudio.instances[0]!
    audio.play.mockReturnValue(playOperation.promise)
    audio.duration = 1
    audio.onloadedmetadata?.()
    await flushOwnedOperations()
    expect(audio.play).toHaveBeenCalledOnce()

    const releasing = tts!.release()
    await expectPending(releasing)
    await expectPending(speaking)
    expect(audio.pause).toHaveBeenCalledOnce()
    expect(closeContext).toHaveBeenCalledOnce()

    playOperation.resolve()
    await expect(releasing).resolves.toBeUndefined()
    await expect(speaking).resolves.toBeNull()
    scope.stop()
    await flushOwnedOperations()
  })

  it("rolls back the first amplitude frame when scheduling synchronously re-enters release", async () => {
    const source = {
      connect: vi.fn(),
      disconnect: vi.fn(),
    }
    const getByteFrequencyData = vi.fn()
    const analyser = {
      fftSize: 0,
      smoothingTimeConstant: 0,
      minDecibels: 0,
      maxDecibels: 0,
      frequencyBinCount: 32,
      connect: vi.fn(),
      disconnect: vi.fn(),
      getByteFrequencyData,
    }
    const closeContext = vi.fn(async () => {})
    class FakeAudioContext {
      state = "running"
      sampleRate = 48_000
      destination = {}
      close = closeContext
      createMediaElementSource = vi.fn(() => source)
      createAnalyser = vi.fn(() => analyser)
    }

    let tts: ReturnType<typeof useTts> | undefined
    let releasing: Promise<void> | null = null
    let cachedFrame: FrameRequestCallback | null = null
    const cancelAnimationFrame = vi.fn()
    vi.stubGlobal("requestAnimationFrame", vi.fn((callback: FrameRequestCallback) => {
      cachedFrame = callback
      releasing = tts!.release()
      return 301
    }))
    vi.stubGlobal("cancelAnimationFrame", cancelAnimationFrame)
    vi.stubGlobal("Audio", FakeAudio)
    vi.stubGlobal("AudioContext", FakeAudioContext)
    vi.stubGlobal("URL", {
      createObjectURL: vi.fn(() => "blob:first-frame"),
      revokeObjectURL: vi.fn(),
    })
    vi.mocked(synthesizeSpeech).mockResolvedValue(new Blob(["audio"]))

    const scope = effectScope()
    tts = scope.run(() => useTts())
    expect(tts).toBeDefined()

    const speaking = tts!.speak("first frame reentry")
    await flushOwnedOperations()
    const audio = FakeAudio.instances[0]!
    audio.duration = 1
    audio.onloadedmetadata?.()
    await flushOwnedOperations()

    await expect(releasing!).resolves.toBeUndefined()
    await expect(speaking).resolves.toBeNull()
    expect(cancelAnimationFrame).toHaveBeenCalledOnce()
    expect(cancelAnimationFrame).toHaveBeenCalledWith(301)
    ;(cachedFrame as FrameRequestCallback | null)?.(16)
    expect(getByteFrequencyData).not.toHaveBeenCalled()
    expect(reportClientEvent).not.toHaveBeenCalledWith(
      "tts_playback_error",
      expect.anything(),
    )
    expect(closeContext).toHaveBeenCalledOnce()
    scope.stop()
    await flushOwnedOperations()
  })

  it("rolls back a synchronously cancelled amplitude re-arm without reviving its cached callback", async () => {
    const source = {
      connect: vi.fn(),
      disconnect: vi.fn(),
    }
    const getByteFrequencyData = vi.fn()
    const analyser = {
      fftSize: 0,
      smoothingTimeConstant: 0,
      minDecibels: 0,
      maxDecibels: 0,
      frequencyBinCount: 32,
      connect: vi.fn(),
      disconnect: vi.fn(),
      getByteFrequencyData,
    }
    const closeContext = vi.fn(async () => {})
    class FakeAudioContext {
      state = "running"
      sampleRate = 48_000
      destination = {}
      close = closeContext
      createMediaElementSource = vi.fn(() => source)
      createAnalyser = vi.fn(() => analyser)
    }

    let tts: ReturnType<typeof useTts> | undefined
    let releasing: Promise<void> | null = null
    const frames = new Map<number, FrameRequestCallback>()
    let nextFrame = 1
    const requestAnimationFrame = vi.fn((callback: FrameRequestCallback) => {
      const id = nextFrame++
      frames.set(id, callback)
      if (id === 2) releasing = tts!.release()
      return id
    })
    const cancelAnimationFrame = vi.fn((id: number) => frames.delete(id))
    vi.stubGlobal("requestAnimationFrame", requestAnimationFrame)
    vi.stubGlobal("cancelAnimationFrame", cancelAnimationFrame)
    vi.stubGlobal("Audio", FakeAudio)
    vi.stubGlobal("AudioContext", FakeAudioContext)
    vi.stubGlobal("URL", {
      createObjectURL: vi.fn(() => "blob:frame-rearm"),
      revokeObjectURL: vi.fn(),
    })
    vi.mocked(synthesizeSpeech).mockResolvedValue(new Blob(["audio"]))

    const scope = effectScope()
    tts = scope.run(() => useTts())
    expect(tts).toBeDefined()

    const speaking = tts!.speak("frame re-arm")
    await flushOwnedOperations()
    const audio = FakeAudio.instances[0]!
    audio.duration = 1
    audio.onloadedmetadata?.()
    await expect(speaking).resolves.toBe(1_000)
    expect(frames.has(1)).toBe(true)

    const firstFrame = frames.get(1)!
    frames.delete(1)
    firstFrame(16)
    await flushOwnedOperations()

    await expect(releasing!).resolves.toBeUndefined()
    expect(requestAnimationFrame).toHaveBeenCalledTimes(2)
    expect(cancelAnimationFrame.mock.calls.map(([id]) => id)).toEqual([2])
    const staleSecondFrame = frames.get(2)
    staleSecondFrame?.(32)
    expect(getByteFrequencyData).toHaveBeenCalledOnce()
    expect(reportClientEvent).not.toHaveBeenCalledWith(
      "tts_playback_error",
      expect.anything(),
    )
    expect(closeContext).toHaveBeenCalledOnce()
    scope.stop()
    await flushOwnedOperations()
  })

  it("rolls back a Web Audio node returned after construction synchronously re-enters release", async () => {
    const source = {
      connect: vi.fn(),
      disconnect: vi.fn(),
    }
    let tts: ReturnType<typeof useTts> | undefined
    let releasing: Promise<void> | null = null
    const closeContext = vi.fn(async () => {})
    const createAnalyser = vi.fn()
    class FakeAudioContext {
      state = "running"
      sampleRate = 48_000
      destination = {}
      close = closeContext
      createMediaElementSource = vi.fn(() => {
        releasing = tts!.release()
        return source
      })
      createAnalyser = createAnalyser
    }

    vi.stubGlobal("Audio", FakeAudio)
    vi.stubGlobal("AudioContext", FakeAudioContext)
    vi.stubGlobal("URL", {
      createObjectURL: vi.fn(() => "blob:late-source-node"),
      revokeObjectURL: vi.fn(),
    })
    vi.mocked(synthesizeSpeech).mockResolvedValue(new Blob(["audio"]))

    const scope = effectScope()
    tts = scope.run(() => useTts())
    expect(tts).toBeDefined()

    const speaking = tts!.speak("late source node")
    await flushOwnedOperations()
    const audio = FakeAudio.instances[0]!
    audio.duration = 1
    audio.onloadedmetadata?.()
    await flushOwnedOperations()

    await expect(releasing!).resolves.toBeUndefined()
    await expect(speaking).resolves.toBeNull()
    expect(source.disconnect).toHaveBeenCalledOnce()
    expect(createAnalyser).not.toHaveBeenCalled()
    expect(audio.pause).toHaveBeenCalledOnce()
    expect(closeContext).toHaveBeenCalledOnce()
    scope.stop()
    await flushOwnedOperations()
  })

  it("retires a context whose close mutated it to closed before rejecting", async () => {
    const closeError = new Error("close reported failure after closing")
    const contexts: FakeAudioContext[] = []
    class FakeAudioContext {
      state: AudioContextState = "running"
      readonly close: ReturnType<typeof vi.fn>

      constructor() {
        const failAfterClosing = contexts.length === 0
        this.close = vi.fn(async () => {
          this.state = "closed"
          if (failAfterClosing) throw closeError
        })
        contexts.push(this)
      }
    }

    vi.stubGlobal("AudioContext", FakeAudioContext)
    vi.mocked(synthesizeSpeech).mockImplementation((_text, signal) => abortableTtsRequest(signal))

    const scope = effectScope()
    const tts = scope.run(() => useTts())
    expect(tts).toBeDefined()

    const first = tts!.speak("第一句")
    await vi.advanceTimersByTimeAsync(0)
    const releasing = tts!.release()
    const [releaseResult, firstResult] = await Promise.allSettled([releasing, first])
    expect(releaseResult.status).toBe("rejected")
    expect(firstResult.status).toBe("rejected")
    if (releaseResult.status !== "rejected" || firstResult.status !== "rejected") return
    expect(releaseResult.reason).toBeInstanceOf(AggregateError)
    expect(releaseResult.reason.message).toBe("TTS teardown failed")
    expect(firstResult.reason).toBe(releaseResult.reason)

    const firstContext = contexts[0]!
    expect(firstContext.state).toBe("closed")
    expect(firstContext.close).toHaveBeenCalledOnce()
    expect(contexts).toHaveLength(1)

    const second = tts!.speak("第二句")
    await vi.waitFor(() => {
      expect(contexts).toHaveLength(2)
      expect(synthesizeSpeech).toHaveBeenCalledTimes(2)
    })
    // Retrying the authoritative owner observes that physical close already
    // completed; it clears the debt without invoking close() a second time.
    expect(firstContext.close).toHaveBeenCalledOnce()

    await expect(tts!.release()).resolves.toBeUndefined()
    await expect(second).resolves.toBeNull()
    expect(contexts[1]!.close).toHaveBeenCalledOnce()
    scope.stop()
    await flushOwnedOperations()
  })

  it("retains one failed metadata listener removal and backpressures the next turn", async () => {
    const closeContext = vi.fn(async () => {})
    class FakeAudioContext {
      state = "running"
      close = closeContext
    }

    vi.stubGlobal("Audio", FakeAudio)
    vi.stubGlobal("AudioContext", FakeAudioContext)
    vi.stubGlobal("URL", {
      createObjectURL: vi.fn(() => `blob:metadata-${FakeAudio.instances.length}`),
      revokeObjectURL: vi.fn(),
    })
    vi.mocked(synthesizeSpeech).mockResolvedValue(new Blob(["audio"]))

    const scope = effectScope()
    const tts = scope.run(() => useTts())
    expect(tts).toBeDefined()

    const first = tts!.speak("第一句")
    await vi.advanceTimersByTimeAsync(0)
    const firstAudio = FakeAudio.instances[0]!
    const loadedHandler = firstAudio.onloadedmetadata
    const removalError = new Error("metadata callback removal failed")
    let storedLoadedHandler = loadedHandler
    const removedHandlers: Array<(() => void) | null> = []
    Object.defineProperty(firstAudio, "onloadedmetadata", {
      configurable: true,
      get: () => storedLoadedHandler,
      set: (handler: (() => void) | null) => {
        removedHandlers.push(storedLoadedHandler)
        if (removedHandlers.length === 1) throw removalError
        storedLoadedHandler = handler
      },
    })

    firstAudio.duration = 1
    loadedHandler?.()
    await expect(first).rejects.toThrow("TTS teardown failed")

    expect(removedHandlers).toEqual([loadedHandler])
    expect(FakeAudio.instances).toHaveLength(1)
    expect(reportClientEvent).toHaveBeenCalledWith(
      "tts_cleanup_error",
      "metadata loaded callback: metadata callback removal failed",
    )

    const second = tts!.speak("第二句")
    expect(synthesizeSpeech).toHaveBeenCalledOnce()
    await vi.waitFor(() => {
      expect(removedHandlers).toEqual([loadedHandler, loadedHandler])
      expect(synthesizeSpeech).toHaveBeenCalledTimes(2)
      expect(FakeAudio.instances).toHaveLength(2)
    })

    await expect(tts!.release()).resolves.toBeUndefined()
    await expect(second).resolves.toBeNull()
    scope.stop()
    await flushOwnedOperations()
    expect(closeContext).toHaveBeenCalledOnce()
  })

  it("rolls back an abort listener whose registration mutated before throwing", async () => {
    const setupError = new Error("abort listener registration failed")
    const removalError = new Error("abort listener removal failed")
    const listeners = new Set<EventListenerOrEventListenerObject>()
    const removedHandlers: EventListenerOrEventListenerObject[] = []
    let addFailed = false
    let removeFailed = false

    vi.spyOn(AbortSignal.prototype, "addEventListener").mockImplementation(
      function (type, listener) {
        if (type !== "abort") return
        listeners.add(listener)
        if (!addFailed) {
          addFailed = true
          throw setupError
        }
      },
    )
    vi.spyOn(AbortSignal.prototype, "removeEventListener").mockImplementation(
      function (type, listener) {
        if (type !== "abort") return
        removedHandlers.push(listener)
        if (!removeFailed) {
          removeFailed = true
          throw removalError
        }
        listeners.delete(listener)
      },
    )

    const closeContext = vi.fn(async () => {})
    class FakeAudioContext {
      state: AudioContextState = "suspended"
      close = closeContext
      resume = vi.fn(async () => {})
    }
    vi.stubGlobal("AudioContext", FakeAudioContext)

    const scope = effectScope()
    const tts = scope.run(() => useTts())
    expect(tts).toBeDefined()

    await expect(tts!.speak("listener setup failure")).rejects.toThrow(
      "TTS teardown failed",
    )
    expect(synthesizeSpeech).not.toHaveBeenCalled()
    expect(removedHandlers).toHaveLength(1)
    expect(listeners).toHaveLength(1)

    await expect(tts!.release()).resolves.toBeUndefined()
    expect(removedHandlers).toHaveLength(2)
    expect(removedHandlers[1]).toBe(removedHandlers[0])
    expect(listeners).toHaveLength(0)
    expect(closeContext).toHaveBeenCalledOnce()

    scope.stop()
    await flushOwnedOperations()
  })

  it("does not recreate timers, requests, or AudioContext after scope disposal", async () => {
    const contexts: FakeAudioContext[] = []
    class FakeAudioContext {
      state = "running"
      close = vi.fn(async () => {})

      constructor() {
        contexts.push(this)
      }
    }

    vi.stubGlobal("AudioContext", FakeAudioContext)
    const scope = effectScope()
    const tts = scope.run(() => useTts())
    expect(tts).toBeDefined()

    scope.stop()
    await flushOwnedOperations()
    await expect(tts!.speak("dispose 後的遲到呼叫")).resolves.toBeNull()

    expect(contexts).toHaveLength(0)
    expect(synthesizeSpeech).not.toHaveBeenCalled()
    expect(tts!.ttsState.value).toBe("idle")
    expect(vi.getTimerCount()).toBe(0)
  })

  it("rolls back both Web Audio nodes exactly once when graph connection fails", async () => {
    let sourceConnected = false
    const disconnectSource = vi.fn()
    const disconnectAnalyser = vi.fn()
    const source = {
      connect: vi.fn(() => {
        sourceConnected = true
        throw new Error("graph connect failed")
      }),
      disconnect: vi.fn(() => {
        expect(sourceConnected).toBe(true)
        sourceConnected = false
        disconnectSource()
      }),
    }
    const analyser = {
      fftSize: 0,
      smoothingTimeConstant: 0,
      minDecibels: 0,
      maxDecibels: 0,
      frequencyBinCount: 32,
      connect: vi.fn(),
      disconnect: disconnectAnalyser,
    }
    const closeContext = vi.fn(async () => {})
    class FakeAudioContext {
      state = "running"
      sampleRate = 48_000
      destination = {}
      close = closeContext
      createMediaElementSource = vi.fn(() => source)
      createAnalyser = vi.fn(() => analyser)
    }

    const revokeObjectURL = vi.fn()
    vi.stubGlobal("Audio", FakeAudio)
    vi.stubGlobal("AudioContext", FakeAudioContext)
    vi.stubGlobal("URL", {
      createObjectURL: vi.fn(() => "blob:tts-graph"),
      revokeObjectURL,
    })
    vi.mocked(synthesizeSpeech).mockResolvedValue(new Blob(["audio"]))

    const scope = effectScope()
    const tts = scope.run(() => useTts())
    expect(tts).toBeDefined()

    const speaking = tts!.speak("你好")
    await vi.advanceTimersByTimeAsync(0)
    const audio = FakeAudio.instances[0]!
    audio.duration = 1
    audio.onloadedmetadata?.()
    await expect(speaking).resolves.toBeNull()

    expect(disconnectSource).toHaveBeenCalledOnce()
    expect(sourceConnected).toBe(false)
    expect(disconnectAnalyser).toHaveBeenCalledOnce()
    expect(audio.pause).toHaveBeenCalledOnce()
    expect(revokeObjectURL).toHaveBeenCalledOnce()
    expect(reportClientEvent).toHaveBeenCalledWith(
      "tts_playback_error",
      "graph connect failed",
    )

    scope.stop()
    await flushOwnedOperations()
    expect(disconnectSource).toHaveBeenCalledOnce()
    expect(disconnectAnalyser).toHaveBeenCalledOnce()
    expect(closeContext).toHaveBeenCalledOnce()
  })

  it("retains a callback teardown reporter failure without a detached rejection", async () => {
    const cleanupError = new Error("audio pause cleanup failed")
    const reportingError = new Error("global reporter failed")
    const reportError = vi.fn(() => {
      throw reportingError
    })
    vi.stubGlobal("reportError", reportError)

    const unhandled = vi.fn((event: PromiseRejectionEvent) => event.preventDefault())
    window.addEventListener("unhandledrejection", unhandled)

    const source = { connect: vi.fn(), disconnect: vi.fn() }
    const analyser = {
      fftSize: 0,
      smoothingTimeConstant: 0,
      minDecibels: 0,
      maxDecibels: 0,
      frequencyBinCount: 32,
      connect: vi.fn(),
      disconnect: vi.fn(),
      getByteFrequencyData: vi.fn(),
    }
    const closeContext = vi.fn(async () => {})
    class FakeAudioContext {
      state: AudioContextState = "running"
      sampleRate = 48_000
      destination = {}
      close = closeContext
      createMediaElementSource = vi.fn(() => source)
      createAnalyser = vi.fn(() => analyser)
    }

    vi.stubGlobal("requestAnimationFrame", vi.fn(() => 701))
    vi.stubGlobal("cancelAnimationFrame", vi.fn())
    vi.stubGlobal("Audio", FakeAudio)
    vi.stubGlobal("AudioContext", FakeAudioContext)
    vi.stubGlobal("URL", {
      createObjectURL: vi.fn(() => "blob:reporter-failure"),
      revokeObjectURL: vi.fn(),
    })
    vi.mocked(synthesizeSpeech).mockResolvedValue(new Blob(["audio"]))

    const scope = effectScope()
    const tts = scope.run(() => useTts())
    expect(tts).toBeDefined()

    const speaking = tts!.speak("reporter failure")
    await flushOwnedOperations()
    const audio = FakeAudio.instances[0]!
    audio.duration = 1
    audio.onloadedmetadata?.()
    await expect(speaking).resolves.toBe(1_000)

    audio.pause.mockImplementationOnce(() => {
      throw cleanupError
    })
    audio.onended?.()
    await flushOwnedOperations()

    expect(reportError).toHaveBeenCalledOnce()
    expect(unhandled).not.toHaveBeenCalled()

    const firstRelease = await Promise.allSettled([tts!.release()])
    expect(firstRelease[0]!.status).toBe("rejected")
    if (firstRelease[0]!.status !== "rejected") return
    expect(nestedErrorMessages(firstRelease[0]!.reason)).toEqual(
      expect.arrayContaining([cleanupError.message, reportingError.message]),
    )
    expect(audio.pause).toHaveBeenCalledTimes(2)
    expect(closeContext).toHaveBeenCalledOnce()

    await expect(tts!.release()).resolves.toBeUndefined()
    expect(reportError).toHaveBeenCalledOnce()
    expect(audio.pause).toHaveBeenCalledTimes(2)
    expect(closeContext).toHaveBeenCalledOnce()

    scope.stop()
    await flushOwnedOperations()
    expect(unhandled).not.toHaveBeenCalled()
    window.removeEventListener("unhandledrejection", unhandled)
  })

  it("fails teardown promptly when turn cancellation does not abort the pending work", async () => {
    const abortFailure = new Error("TTS turn abort failed before mutation")
    const nativeAbort = AbortController.prototype.abort
    let abortCalls = 0
    vi.spyOn(AbortController.prototype, "abort").mockImplementation(
      function (reason?: unknown) {
        abortCalls += 1
        if (abortCalls === 1) throw abortFailure
        nativeAbort.call(this, reason)
      },
    )

    const closeContext = vi.fn(async () => {})
    class FakeAudioContext {
      state: AudioContextState = "running"
      close = closeContext
    }
    vi.stubGlobal("AudioContext", FakeAudioContext)
    vi.mocked(synthesizeSpeech).mockImplementation((_text, signal) => abortableTtsRequest(signal))

    const scope = effectScope()
    const tts = scope.run(() => useTts())
    expect(tts).toBeDefined()

    const speaking = tts!.speak("uncancelled request")
    await flushOwnedOperations()

    const firstRelease = await Promise.allSettled([tts!.release()])
    expect(firstRelease[0]!.status).toBe("rejected")
    if (firstRelease[0]!.status !== "rejected") return
    expect(nestedErrorMessages(firstRelease[0]!.reason)).toEqual(
      expect.arrayContaining([
        abortFailure.message,
        "TTS turn cancellation debt remains unresolved",
      ]),
    )
    await expectPending(speaking)

    await expect(tts!.release()).resolves.toBeUndefined()
    await expect(speaking).resolves.toBeNull()
    expect(abortCalls).toBe(3)
    expect(closeContext).toHaveBeenCalledOnce()

    scope.stop()
    await flushOwnedOperations()
  })

  it("does not retry a failed AudioContext close gate within one release attempt", async () => {
    const abortFailure = new Error("AudioContext gate abort failed")
    const nativeAbort = AbortController.prototype.abort
    let abortCalls = 0
    vi.spyOn(AbortController.prototype, "abort").mockImplementation(
      function (reason?: unknown) {
        abortCalls += 1
        nativeAbort.call(this, reason)
        if (abortCalls === 2) throw abortFailure
      },
    )

    const closeContext = vi.fn(async () => {})
    class FakeAudioContext {
      state: AudioContextState = "running"
      close = closeContext
    }
    vi.stubGlobal("AudioContext", FakeAudioContext)
    vi.mocked(synthesizeSpeech).mockImplementation((_text, signal) => abortableTtsRequest(signal))

    const scope = effectScope()
    const tts = scope.run(() => useTts())
    expect(tts).toBeDefined()

    const speaking = tts!.speak("same release attempt")
    await flushOwnedOperations()
    const firstRelease = await Promise.allSettled([tts!.release(), speaking])
    expect(firstRelease[0]!.status).toBe("rejected")
    expect(firstRelease[1]!.status).toBe("rejected")
    expect(abortCalls).toBe(2)
    expect(closeContext).toHaveBeenCalledOnce()

    await expect(tts!.release()).resolves.toBeUndefined()
    expect(abortCalls).toBe(3)
    expect(closeContext).toHaveBeenCalledOnce()

    scope.stop()
    await flushOwnedOperations()
  })

  it("aggregates an AudioContext state failure with gate rollback debt", async () => {
    const stateFailure = new Error("AudioContext state getter failed")
    const abortFailure = new Error("AudioContext rollback abort failed")
    const nativeAbort = AbortController.prototype.abort
    let abortCalls = 0
    vi.spyOn(AbortController.prototype, "abort").mockImplementation(
      function (reason?: unknown) {
        abortCalls += 1
        nativeAbort.call(this, reason)
        if (abortCalls === 1) throw abortFailure
      },
    )

    const closeContext = vi.fn(async () => {})
    class FakeAudioContext {
      private stateReads = 0
      private physicalState: AudioContextState = "running"

      get state(): AudioContextState {
        this.stateReads += 1
        if (this.stateReads === 1) throw stateFailure
        return this.physicalState
      }

      close = vi.fn(async () => {
        this.physicalState = "closed"
        await closeContext()
      })
    }
    vi.stubGlobal("AudioContext", FakeAudioContext)

    const scope = effectScope()
    const tts = scope.run(() => useTts())
    expect(tts).toBeDefined()

    const result = await Promise.allSettled([tts!.speak("state failure")])
    expect(result[0]!.status).toBe("rejected")
    if (result[0]!.status !== "rejected") return
    expect(nestedErrorMessages(result[0]!.reason)).toEqual(
      expect.arrayContaining([stateFailure.message, abortFailure.message]),
    )
    expect(abortCalls).toBe(2)
    expect(closeContext).not.toHaveBeenCalled()

    await expect(tts!.release()).resolves.toBeUndefined()
    expect(abortCalls).toBe(3)
    expect(closeContext).toHaveBeenCalledOnce()

    scope.stop()
    await flushOwnedOperations()
  })
})
