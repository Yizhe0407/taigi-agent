import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

const originalSendBeacon = Object.getOwnPropertyDescriptor(navigator, "sendBeacon")

function installSendBeacon(implementation: () => boolean) {
  Object.defineProperty(navigator, "sendBeacon", {
    configurable: true,
    value: vi.fn(implementation),
  })
}

async function flushDelivery() {
  await Promise.resolve()
  await Promise.resolve()
}

function abortablePendingFetch() {
  return vi.fn((_url: RequestInfo | URL, init?: RequestInit) => new Promise<Response>((_resolve, reject) => {
    init?.signal?.addEventListener("abort", () => {
      reject(new DOMException("Aborted", "AbortError"))
    }, { once: true })
  }))
}

describe("reportClientEvent delivery ownership", () => {
  beforeEach(() => {
    vi.resetModules()
  })

  afterEach(() => {
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
    if (originalSendBeacon) {
      Object.defineProperty(navigator, "sendBeacon", originalSendBeacon)
    } else {
      Reflect.deleteProperty(navigator, "sendBeacon")
    }
  })

  it("observes a rejected fetch fallback without producing an unhandled rejection", async () => {
    installSendBeacon(() => false)
    const fetch = vi.fn().mockRejectedValue(new Error("offline"))
    vi.stubGlobal("fetch", fetch)
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => {})
    const { reportClientEvent } = await import("@/lib/report-client-event")

    reportClientEvent("test_event", "network failure")
    await flushDelivery()

    expect(fetch).toHaveBeenCalledOnce()
    expect(consoleError).toHaveBeenCalledOnce()
    expect(consoleError).toHaveBeenCalledWith(
      "Client event delivery failed:",
      "offline",
    )
  })

  it("treats a non-2xx fallback response as an observable terminal failure", async () => {
    installSendBeacon(() => false)
    vi.stubGlobal("fetch", vi.fn(async () => new Response(null, { status: 503 })))
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => {})
    const { reportClientEvent } = await import("@/lib/report-client-event")

    reportClientEvent("test_event", "server failure")
    await flushDelivery()

    expect(consoleError).toHaveBeenCalledWith(
      "Client event delivery failed:",
      "POST /api/client-events -> 503",
    )
  })

  it("transfers ownership to fetch when sendBeacon throws", async () => {
    installSendBeacon(() => { throw new Error("beacon unavailable") })
    const fetch = vi.fn(async () => new Response(null, { status: 204 }))
    vi.stubGlobal("fetch", fetch)
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => {})
    const { reportClientEvent } = await import("@/lib/report-client-event")

    expect(() => reportClientEvent("test_event", "fallback succeeds")).not.toThrow()
    await flushDelivery()

    expect(fetch).toHaveBeenCalledOnce()
    expect(consoleError).not.toHaveBeenCalled()
  })

  it("terminal shutdown aborts owned fallbacks and permanently closes acquisition", async () => {
    installSendBeacon(() => false)
    const fetch = abortablePendingFetch()
    vi.stubGlobal("fetch", fetch)
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => {})
    const {
      reportClientEvent,
      shutdownClientEventReporting,
    } = await import("@/lib/report-client-event")

    reportClientEvent("test_event", "in flight")
    await vi.waitFor(() => expect(fetch).toHaveBeenCalledOnce())
    const signal = fetch.mock.calls[0]?.[1]?.signal
    expect(signal?.aborted).toBe(false)

    const firstShutdown = shutdownClientEventReporting()
    const secondShutdown = shutdownClientEventReporting()
    expect(secondShutdown).toBe(firstShutdown)
    await firstShutdown

    expect(signal?.aborted).toBe(true)
    reportClientEvent("test_event", "late callback")
    expect(fetch).toHaveBeenCalledOnce()
    expect(consoleError).not.toHaveBeenCalled()
  })

  it("bounds unresolved fallback ownership across rate-limit windows", async () => {
    installSendBeacon(() => false)
    const fetch = abortablePendingFetch()
    vi.stubGlobal("fetch", fetch)
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => {})
    let now = 0
    vi.spyOn(Date, "now").mockImplementation(() => now)
    const {
      reportClientEvent,
      shutdownClientEventReporting,
    } = await import("@/lib/report-client-event")

    for (let index = 0; index < 11; index++) {
      now += 61_000
      reportClientEvent("test_event", `message-${index}`)
    }

    await vi.waitFor(() => expect(fetch).toHaveBeenCalledTimes(10))
    expect(consoleError).toHaveBeenCalledWith(
      "Client event delivery skipped:",
      "already 10 fallback requests in flight",
    )

    await shutdownClientEventReporting()
  })

  it("keeps shutdown pending until the exact fallback promise settles", async () => {
    installSendBeacon(() => false)
    let rejectDelivery!: (reason?: unknown) => void
    const fetch = vi.fn(
      (_url: RequestInfo | URL, init?: RequestInit) =>
        new Promise<Response>((_resolve, reject) => {
          rejectDelivery = reject
          init?.signal?.addEventListener("abort", () => undefined, {
            once: true,
          })
        }),
    )
    vi.stubGlobal("fetch", fetch)
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => {})
    const {
      reportClientEvent,
      shutdownClientEventReporting,
    } = await import("@/lib/report-client-event")

    reportClientEvent("test_event", "physical request")
    await vi.waitFor(() => expect(fetch).toHaveBeenCalledOnce())
    const signal = fetch.mock.calls[0]?.[1]?.signal

    const shutdown = shutdownClientEventReporting()
    let shutdownSettled = false
    shutdown.then(() => {
      shutdownSettled = true
    })
    await flushDelivery()

    expect(signal?.aborted).toBe(true)
    expect(shutdownSettled).toBe(false)

    rejectDelivery(new DOMException("Aborted", "AbortError"))
    await shutdown

    expect(shutdownSettled).toBe(true)
    expect(consoleError).not.toHaveBeenCalled()
  })
  it("publishes fallback ownership before fetch can re-enter shutdown", async () => {
    installSendBeacon(() => false)
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => {})
    let shutdownClientEventReporting!: () => Promise<void>
    let shutdown: Promise<void> | null = null
    const fetch = vi.fn((_url: RequestInfo | URL, init?: RequestInit) => {
      shutdown = shutdownClientEventReporting()
      return new Promise<Response>((_resolve, reject) => {
        if (init?.signal?.aborted) {
          reject(new DOMException("Aborted", "AbortError"))
          return
        }
        init?.signal?.addEventListener("abort", () => {
          reject(new DOMException("Aborted", "AbortError"))
        }, { once: true })
      })
    })
    vi.stubGlobal("fetch", fetch)
    const module = await import("@/lib/report-client-event")
    shutdownClientEventReporting = module.shutdownClientEventReporting

    module.reportClientEvent("test_event", "reentrant shutdown")
    await vi.waitFor(() => expect(fetch).toHaveBeenCalledOnce())
    await shutdown

    const signal = fetch.mock.calls[0]?.[1]?.signal
    expect(signal?.aborted).toBe(true)
    expect(consoleError).not.toHaveBeenCalled()
  })

  it("returns a failed abort attempt without waiting forever and retries the live delivery", async () => {
    installSendBeacon(() => false)
    const abortError = new Error("abort failed before cancellation")
    const NativeAbortController = globalThis.AbortController
    const controllers: Array<{ signal: AbortSignal; abort: ReturnType<typeof vi.fn> }> = []

    class RetryableAbortController {
      readonly inner = new NativeAbortController()
      readonly signal = this.inner.signal
      readonly abort = vi.fn(() => {
        if (this.abort.mock.calls.length === 1) throw abortError
        this.inner.abort()
      })

      constructor() {
        controllers.push(this)
      }
    }

    vi.stubGlobal("AbortController", RetryableAbortController)
    const fetch = abortablePendingFetch()
    vi.stubGlobal("fetch", fetch)
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => {})
    const {
      reportClientEvent,
      shutdownClientEventReporting,
    } = await import("@/lib/report-client-event")

    reportClientEvent("test_event", "retry pending abort")
    await vi.waitFor(() => expect(fetch).toHaveBeenCalledOnce())
    const controller = controllers[0]!

    await expect(shutdownClientEventReporting()).rejects.toBe(abortError)
    expect(controller.abort).toHaveBeenCalledOnce()
    expect(controller.signal.aborted).toBe(false)

    await expect(shutdownClientEventReporting()).resolves.toBeUndefined()
    expect(controller.abort).toHaveBeenCalledTimes(2)
    expect(controller.signal.aborted).toBe(true)
    expect(consoleError).not.toHaveBeenCalled()
  })

  it("retains abort cleanup debt and retries the same controller after delivery settles", async () => {
    installSendBeacon(() => false)
    const abortError = new Error("abort failed")
    const NativeAbortController = globalThis.AbortController
    const controllers: Array<{ signal: AbortSignal; abort: ReturnType<typeof vi.fn> }> = []

    class RetryableAbortController {
      readonly inner = new NativeAbortController()
      readonly signal = this.inner.signal
      readonly abort = vi.fn(() => {
        if (this.abort.mock.calls.length === 1) throw abortError
        this.inner.abort()
      })

      constructor() {
        controllers.push(this)
      }
    }

    vi.stubGlobal("AbortController", RetryableAbortController)
    let resolveFetch!: (response: Response) => void
    const fetch = vi.fn(() => new Promise<Response>((resolve) => {
      resolveFetch = resolve
    }))
    vi.stubGlobal("fetch", fetch)
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => {})
    const {
      reportClientEvent,
      shutdownClientEventReporting,
    } = await import("@/lib/report-client-event")

    reportClientEvent("test_event", "retry abort")
    await vi.waitFor(() => expect(fetch).toHaveBeenCalledOnce())
    const controller = controllers[0]!

    const firstShutdown = shutdownClientEventReporting()
    resolveFetch(new Response(null, { status: 204 }))
    await expect(firstShutdown).rejects.toBe(abortError)
    expect(controller.abort).toHaveBeenCalledOnce()

    await expect(shutdownClientEventReporting()).resolves.toBeUndefined()
    expect(controller.abort).toHaveBeenCalledTimes(2)
    expect(controller.signal.aborted).toBe(true)
    expect(consoleError).not.toHaveBeenCalled()
  })

  it("uses one document owner signal instead of constructing per-delivery controllers", async () => {
    installSendBeacon(() => false)
    const NativeAbortController = globalThis.AbortController
    const controllers: Array<{ signal: AbortSignal; abort(): void }> = []

    class DocumentAbortController {
      readonly inner = new NativeAbortController()
      readonly signal = this.inner.signal

      constructor() {
        controllers.push(this)
        if (controllers.length > 1) {
          throw new Error("per-delivery AbortController construction is forbidden")
        }
      }

      abort(): void {
        this.inner.abort()
      }
    }

    vi.stubGlobal("AbortController", DocumentAbortController)
    const fetch = abortablePendingFetch()
    vi.stubGlobal("fetch", fetch)
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => {})
    const {
      reportClientEvent,
      shutdownClientEventReporting,
    } = await import("@/lib/report-client-event")

    reportClientEvent("test_event", "first shared delivery")
    reportClientEvent("test_event", "second shared delivery")
    await vi.waitFor(() => expect(fetch).toHaveBeenCalledTimes(2))

    expect(controllers).toHaveLength(1)
    expect(fetch.mock.calls[0]?.[1]?.signal).toBe(controllers[0]?.signal)
    expect(fetch.mock.calls[1]?.[1]?.signal).toBe(controllers[0]?.signal)

    await shutdownClientEventReporting()
    expect(controllers[0]?.signal.aborted).toBe(true)
    expect(consoleError).not.toHaveBeenCalled()
  })

  it("publishes shutdown before AbortController abort can re-enter it", async () => {
    installSendBeacon(() => false)
    const NativeAbortController = globalThis.AbortController
    let shutdownClientEventReporting!: () => Promise<void>
    let reentrantShutdown: Promise<void> | null = null

    class ReentrantAbortController {
      readonly inner = new NativeAbortController()
      readonly signal = this.inner.signal

      abort(): void {
        reentrantShutdown = shutdownClientEventReporting()
        this.inner.abort()
      }
    }

    vi.stubGlobal("AbortController", ReentrantAbortController)
    const fetch = abortablePendingFetch()
    vi.stubGlobal("fetch", fetch)
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => {})
    const module = await import("@/lib/report-client-event")
    shutdownClientEventReporting = module.shutdownClientEventReporting

    module.reportClientEvent("test_event", "abort re-entry")
    await vi.waitFor(() => expect(fetch).toHaveBeenCalledOnce())

    const shutdown = shutdownClientEventReporting()
    expect(reentrantShutdown).toBe(shutdown)
    await shutdown
    expect(consoleError).not.toHaveBeenCalled()
  })

  it("retains a null AbortController failure for a distinct shutdown attempt", async () => {
    installSendBeacon(() => false)
    const NativeAbortController = globalThis.AbortController
    const inner = new NativeAbortController()
    const abort = vi.fn(() => {
      if (abort.mock.calls.length === 1) throw null
      inner.abort()
    })

    vi.stubGlobal("AbortController", class {
      readonly signal = inner.signal
      readonly abort = abort
    })
    const fetch = abortablePendingFetch()
    vi.stubGlobal("fetch", fetch)
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => {})
    const {
      reportClientEvent,
      shutdownClientEventReporting,
    } = await import("@/lib/report-client-event")
    const firstAttempt = {}

    reportClientEvent("test_event", "null abort debt")
    await vi.waitFor(() => expect(fetch).toHaveBeenCalledOnce())

    await expect(shutdownClientEventReporting(firstAttempt)).rejects.toBeNull()
    await expect(shutdownClientEventReporting(firstAttempt)).rejects.toThrow(
      "Client event reporting cleanup remains unresolved",
    )
    expect(abort).toHaveBeenCalledOnce()

    await expect(shutdownClientEventReporting({})).resolves.toBeUndefined()
    expect(abort).toHaveBeenCalledTimes(2)
    expect(consoleError).not.toHaveBeenCalled()
  })

  it("never retries a failed exact shutdown attempt", async () => {
    installSendBeacon(() => false)
    const abortError = new Error("abort failed before cancellation")
    const NativeAbortController = globalThis.AbortController
    const inner = new NativeAbortController()
    const abort = vi.fn(() => {
      if (abort.mock.calls.length === 1) throw abortError
      inner.abort()
    })

    vi.stubGlobal("AbortController", class {
      readonly signal = inner.signal
      readonly abort = abort
    })
    const fetch = abortablePendingFetch()
    vi.stubGlobal("fetch", fetch)
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => {})
    const {
      reportClientEvent,
      shutdownClientEventReporting,
    } = await import("@/lib/report-client-event")
    const firstAttempt = {}

    reportClientEvent("test_event", "attempt fencing")
    await vi.waitFor(() => expect(fetch).toHaveBeenCalledOnce())

    await expect(shutdownClientEventReporting(firstAttempt)).rejects.toBe(abortError)
    await expect(shutdownClientEventReporting(firstAttempt)).rejects.toThrow(
      "Client event reporting cleanup remains unresolved",
    )
    expect(abort).toHaveBeenCalledOnce()

    await expect(shutdownClientEventReporting({})).resolves.toBeUndefined()
    expect(abort).toHaveBeenCalledTimes(2)
    expect(consoleError).not.toHaveBeenCalled()
  })

})
