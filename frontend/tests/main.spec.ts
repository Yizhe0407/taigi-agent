import { effectScope, type App } from "vue"
import { afterEach, describe, expect, it, vi } from "vitest"

import {
  observeAsynchronousScopeTeardown,
  observeSynchronousScopeTeardown,
} from "@/lib/component-lifecycle"
import { createApplicationLifecycle } from "@/lib/application-lifecycle"

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
  if (!(error instanceof Error)) return [String(error)]
  const messages = [error.message]
  if (error instanceof AggregateError) {
    for (const nested of error.errors) messages.push(...nestedErrorMessages(nested))
  }
  if (error.cause !== undefined) messages.push(...nestedErrorMessages(error.cause))
  return messages
}

function pageHideEvent(persisted = false): PageTransitionEvent {
  const event = new Event("pagehide") as PageTransitionEvent
  Object.defineProperty(event, "persisted", { value: persisted })
  return event
}

function rejectionEvent(reason: unknown): PromiseRejectionEvent {
  const event = new Event("unhandledrejection") as PromiseRejectionEvent
  Object.defineProperty(event, "reason", { value: reason })
  return event
}

function createAppHarness() {
  const mount = vi.fn()
  const unmount = vi.fn()
  const errorHandler = vi.fn()
  const app = {
    config: { errorHandler },
    mount,
    unmount,
  } as unknown as App
  return { app, mount, unmount, errorHandler }
}

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe("application document lifecycle", () => {
  it("joins terminal pagehide teardown and permanently gates cached callbacks", async () => {
    const { app, mount, unmount } = createAppHarness()
    const reportClientEvent = vi.fn()
    const shutdown = deferred<void>()
    const shutdownClientEventReporting = vi.fn(() => shutdown.promise)
    const addEventListener = vi.spyOn(window, "addEventListener")
    const removeEventListener = vi.spyOn(window, "removeEventListener")
    const lifecycle = createApplicationLifecycle({
      app,
      mountTarget: "#app",
      reportClientEvent,
      shutdownClientEventReporting,
    })

    await lifecycle.start()
    expect(lifecycle.started).toBe(true)
    expect(mount).toHaveBeenCalledWith("#app")

    const errorHandler = addEventListener.mock.calls.find(([type]) => type === "error")?.[1]
    const rejectionHandler = addEventListener.mock.calls.find(
      ([type]) => type === "unhandledrejection",
    )?.[1]
    const pagehideHandler = addEventListener.mock.calls.find(
      ([type]) => type === "pagehide",
    )?.[1]
    expect(typeof errorHandler).toBe("function")
    expect(typeof rejectionHandler).toBe("function")
    expect(typeof pagehideHandler).toBe("function")

    if (typeof errorHandler === "function") {
      errorHandler(new ErrorEvent("error", { message: "visible failure" }))
    }
    if (typeof rejectionHandler === "function") {
      rejectionHandler(rejectionEvent(new Error("rejected operation")))
    }
    expect(reportClientEvent).toHaveBeenCalledTimes(2)

    if (typeof pagehideHandler === "function") pagehideHandler(pageHideEvent(true))
    expect(lifecycle.closing).toBe(false)
    expect(shutdownClientEventReporting).not.toHaveBeenCalled()

    if (typeof pagehideHandler === "function") pagehideHandler(pageHideEvent())
    expect(lifecycle.closing).toBe(true)
    await vi.waitFor(() => expect(shutdownClientEventReporting).toHaveBeenCalledOnce())
    expect(unmount).toHaveBeenCalledOnce()
    expect(removeEventListener).toHaveBeenCalledWith("error", errorHandler)
    expect(removeEventListener).toHaveBeenCalledWith(
      "unhandledrejection",
      rejectionHandler,
    )
    expect(removeEventListener).toHaveBeenCalledWith("pagehide", pagehideHandler)

    const joinedTeardown = lifecycle.teardown()
    shutdown.resolve()
    await joinedTeardown
    expect(lifecycle.started).toBe(false)

    if (typeof errorHandler === "function") {
      errorHandler(new ErrorEvent("error", { message: "late failure" }))
    }
    if (typeof rejectionHandler === "function") {
      rejectionHandler(rejectionEvent(new Error("late rejection")))
    }
    if (typeof pagehideHandler === "function") pagehideHandler(pageHideEvent())
    await lifecycle.teardown()
    expect(reportClientEvent).toHaveBeenCalledTimes(2)
    expect(unmount).toHaveBeenCalledOnce()
    expect(shutdownClientEventReporting).toHaveBeenCalledOnce()
  })

  it("publishes observed teardown before AbortController re-entry", async () => {
    let lifecycle: ReturnType<typeof createApplicationLifecycle> | null = null
    let reentered = false
    class ReentrantAbortController extends AbortController {
      override abort(reason?: unknown): void {
        super.abort(reason)
        if (!reentered) {
          reentered = true
          lifecycle?.requestObservedTeardown("reentrant observed teardown")
        }
      }
    }
    vi.stubGlobal("AbortController", ReentrantAbortController)

    const { app, unmount } = createAppHarness()
    const shutdownClientEventReporting = vi.fn(async () => undefined)
    lifecycle = createApplicationLifecycle({
      app,
      mountTarget: "#app",
      reportClientEvent: vi.fn(),
      shutdownClientEventReporting,
    })
    await lifecycle.start()

    lifecycle.requestObservedTeardown("outer observed teardown")
    await expect(lifecycle.teardown()).resolves.toBeUndefined()

    expect(reentered).toBe(true)
    expect(unmount).toHaveBeenCalledOnce()
    expect(shutdownClientEventReporting).toHaveBeenCalledOnce()
  })

  it("rolls back a listener that registers before addEventListener throws and retries the same claim", async () => {
    const { app, mount, unmount } = createAppHarness()
    const reportClientEvent = vi.fn()
    const shutdownClientEventReporting = vi.fn(async () => {})
    const setupError = new Error("listener registration failed")
    const cleanupError = new Error("listener removal failed")
    const originalAddEventListener = window.addEventListener.bind(window)
    const addEventListener = vi.spyOn(window, "addEventListener").mockImplementationOnce(
      ((type: string, listener: EventListenerOrEventListenerObject, options?: boolean | AddEventListenerOptions) => {
        originalAddEventListener(type, listener, options)
        throw setupError
      }) as typeof window.addEventListener,
    )
    const removeEventListener = vi.spyOn(window, "removeEventListener")
    removeEventListener.mockImplementationOnce(() => {
      throw cleanupError
    })
    const lifecycle = createApplicationLifecycle({
      app,
      mountTarget: "#app",
      reportClientEvent,
      shutdownClientEventReporting,
    })

    await expect(lifecycle.start()).rejects.toMatchObject({
      message: "Failed to acquire and roll back window error listener",
    })
    const errorHandler = addEventListener.mock.calls[0]?.[1]
    expect(mount).not.toHaveBeenCalled()
    expect(unmount).not.toHaveBeenCalled()
    expect(removeEventListener.mock.calls).toHaveLength(1)
    expect(removeEventListener.mock.calls[0]?.[1]).toBe(errorHandler)
    expect(lifecycle.closing).toBe(true)
    expect(shutdownClientEventReporting).toHaveBeenCalledOnce()

    await lifecycle.teardown()
    expect(removeEventListener.mock.calls).toHaveLength(2)
    expect(removeEventListener.mock.calls[1]?.[1]).toBe(errorHandler)
    expect(shutdownClientEventReporting).toHaveBeenCalledOnce()

    if (typeof errorHandler === "function") {
      errorHandler(new ErrorEvent("error", { message: "cached callback" }))
    }
    expect(reportClientEvent).not.toHaveBeenCalled()
  })

  it("pre-publishes unmount before a mutating mount and retries only the failed rollback", async () => {
    const { app, mount, unmount } = createAppHarness()
    const reportClientEvent = vi.fn()
    const shutdownClientEventReporting = vi.fn(async () => {})
    const mountError = new Error("mount failed after mutation")
    const unmountError = new Error("unmount failed")
    mount.mockImplementation(() => {
      throw mountError
    })
    unmount.mockImplementationOnce(() => {
      throw unmountError
    })
    const addEventListener = vi.spyOn(window, "addEventListener")
    const removeEventListener = vi.spyOn(window, "removeEventListener")
    const lifecycle = createApplicationLifecycle({
      app,
      mountTarget: "#app",
      reportClientEvent,
      shutdownClientEventReporting,
    })

    await expect(lifecycle.start()).rejects.toMatchObject({
      message: "Failed to acquire and roll back Vue application mount",
    })
    expect(mount).toHaveBeenCalledOnce()
    expect(unmount).toHaveBeenCalledOnce()
    expect(removeEventListener).toHaveBeenCalledTimes(3)
    const removedHandlers = new Set(removeEventListener.mock.calls.map(([, handler]) => handler))
    expect(removedHandlers).toEqual(
      new Set(addEventListener.mock.calls.map(([, handler]) => handler)),
    )

    expect(shutdownClientEventReporting).toHaveBeenCalledOnce()
    await lifecycle.teardown()
    expect(unmount).toHaveBeenCalledTimes(2)
    expect(removeEventListener).toHaveBeenCalledTimes(3)
    expect(shutdownClientEventReporting).toHaveBeenCalledOnce()
  })

  it("reports observed HMR teardown failures through Vue and retries exact remaining debt", async () => {
    const { app, unmount, errorHandler } = createAppHarness()
    const reportClientEvent = vi.fn()
    const shutdownError = new Error("client reporting shutdown failed")
    const shutdownClientEventReporting = vi.fn()
      .mockRejectedValueOnce(shutdownError)
      .mockResolvedValue(undefined)
    const addEventListener = vi.spyOn(window, "addEventListener")
    const removeEventListener = vi.spyOn(window, "removeEventListener")
    const removalError = new Error("pagehide removal failed")
    removeEventListener.mockImplementationOnce(() => {
      throw removalError
    })
    const lifecycle = createApplicationLifecycle({
      app,
      mountTarget: "#app",
      reportClientEvent,
      shutdownClientEventReporting,
    })
    await lifecycle.start()
    const pagehideHandler = addEventListener.mock.calls.find(
      ([type]) => type === "pagehide",
    )?.[1]

    lifecycle.requestObservedTeardown("application HMR teardown")
    lifecycle.requestObservedTeardown("duplicate HMR teardown")
    await vi.waitFor(() => expect(errorHandler).toHaveBeenCalledOnce())

    expect(errorHandler).toHaveBeenCalledWith(
      expect.objectContaining({ message: "Application teardown failed" }),
      null,
      "application HMR teardown",
    )
    expect(unmount).toHaveBeenCalledOnce()
    const firstPagehideRemoval = removeEventListener.mock.calls.find(
      ([type]) => type === "pagehide",
    )
    expect(firstPagehideRemoval?.[1]).toBe(pagehideHandler)

    await lifecycle.teardown()
    const pagehideRemovals = removeEventListener.mock.calls.filter(
      ([type]) => type === "pagehide",
    )
    expect(pagehideRemovals).toHaveLength(2)
    expect(pagehideRemovals[0]?.[1]).toBe(pagehideHandler)
    expect(pagehideRemovals[1]?.[1]).toBe(pagehideHandler)
    expect(unmount).toHaveBeenCalledOnce()
    expect(shutdownClientEventReporting).toHaveBeenCalledTimes(2)
  })

  it("retains a throwing observed-teardown reporter without an unhandled rejection", async () => {
    const { app, errorHandler, unmount } = createAppHarness()
    const cleanupError = new Error("observed pagehide removal failed")
    const handlerError = new Error("application error handler failed")
    const globalReporterError = new Error("global error reporter failed")
    errorHandler.mockImplementation(() => {
      throw handlerError
    })
    vi.stubGlobal("reportError", vi.fn(() => {
      throw globalReporterError
    }))
    const removeEventListener = vi.spyOn(window, "removeEventListener")
    removeEventListener.mockImplementationOnce(() => {
      throw cleanupError
    })
    const unhandled = vi.fn()
    window.addEventListener("unhandledrejection", unhandled)
    const lifecycle = createApplicationLifecycle({
      app,
      mountTarget: "#app",
      reportClientEvent: vi.fn(),
      shutdownClientEventReporting: vi.fn(async () => {}),
    })
    await lifecycle.start()

    lifecycle.requestObservedTeardown("application reporter failure")
    await vi.waitFor(() => expect(errorHandler).toHaveBeenCalledOnce())

    let retainedFailure: unknown
    try {
      await lifecycle.teardown()
    } catch (error) {
      retainedFailure = error
    }
    expect(nestedErrorMessages(retainedFailure)).toEqual(expect.arrayContaining([
      expect.stringContaining("Application observed teardown"),
      cleanupError.message,
      handlerError.message,
      globalReporterError.message,
    ]))
    expect(unmount).toHaveBeenCalledOnce()
    expect(unhandled).not.toHaveBeenCalled()
    await expect(lifecycle.teardown()).resolves.toBeUndefined()

    window.removeEventListener("unhandledrejection", unhandled)
  })

  it("defers component debt created by the current unmount until the next teardown attempt", async () => {
    const cleanupError = new Error("component cleanup failed")
    const release = vi
      .fn<() => void>()
      .mockImplementationOnce(() => {
        throw cleanupError
      })
    const reportError = vi.fn()
    vi.stubGlobal("reportError", reportError)
    const componentScope = effectScope()
    componentScope.run(() => {
      observeSynchronousScopeTeardown(
        "application child teardown",
        () => release(),
      )
    })

    const { app, unmount } = createAppHarness()
    unmount.mockImplementation(() => componentScope.stop())
    const lifecycle = createApplicationLifecycle({
      app,
      mountTarget: "#app",
      reportClientEvent: vi.fn(),
      shutdownClientEventReporting: vi.fn(async () => {}),
    })
    await lifecycle.start()

    await expect(lifecycle.teardown()).rejects.toThrow(
      "application child teardown: component cleanup failed",
    )
    expect(release).toHaveBeenCalledOnce()
    expect(unmount).toHaveBeenCalledOnce()
    expect(reportError).toHaveBeenCalledOnce()

    await expect(lifecycle.teardown()).resolves.toBeUndefined()
    expect(release).toHaveBeenCalledTimes(2)
    expect(unmount).toHaveBeenCalledOnce()
    expect(reportError).toHaveBeenCalledOnce()
  })

  it("joins synchronous cleanup and reporter failures without replaying settled claims", async () => {
    const cleanupError = new Error("component cleanup failed before retry")
    const reportingError = new Error("component cleanup reporter failed")
    const release = vi
      .fn<() => void>()
      .mockImplementationOnce(() => {
        throw cleanupError
      })
      .mockImplementationOnce(() => undefined)
    const reportError = vi.fn(() => {
      throw reportingError
    })
    vi.stubGlobal("reportError", reportError)
    const componentScope = effectScope()
    componentScope.run(() => {
      observeSynchronousScopeTeardown(
        "application throwing child reporter",
        () => release(),
      )
    })

    const { app, unmount } = createAppHarness()
    unmount.mockImplementation(() => componentScope.stop())
    const shutdownClientEventReporting = vi.fn(async () => {})
    const lifecycle = createApplicationLifecycle({
      app,
      mountTarget: "#app",
      reportClientEvent: vi.fn(),
      shutdownClientEventReporting,
    })
    await lifecycle.start()

    let retainedFailure: unknown
    try {
      await lifecycle.teardown()
    } catch (error) {
      retainedFailure = error
    }
    expect(nestedErrorMessages(retainedFailure)).toEqual(expect.arrayContaining([
      expect.stringContaining("application throwing child reporter"),
      cleanupError.message,
      reportingError.message,
    ]))
    expect(release).toHaveBeenCalledOnce()
    expect(reportError).toHaveBeenCalledOnce()
    expect(unmount).toHaveBeenCalledOnce()
    expect(shutdownClientEventReporting).toHaveBeenCalledOnce()

    await expect(lifecycle.teardown()).resolves.toBeUndefined()
    expect(release).toHaveBeenCalledTimes(2)
    expect(reportError).toHaveBeenCalledOnce()
    expect(unmount).toHaveBeenCalledOnce()
    expect(shutdownClientEventReporting).toHaveBeenCalledOnce()
    await expect(lifecycle.teardown()).resolves.toBeUndefined()
  })

  it("forwards one exact application attempt through component and reporting teardown", async () => {
    const componentAttempts: ResourceReleaseAttempt[] = []
    const componentScope = effectScope()
    componentScope.run(() => {
      observeSynchronousScopeTeardown(
        "application attempt propagation",
        attempt => componentAttempts.push(attempt),
      )
    })

    const { app, unmount } = createAppHarness()
    unmount.mockImplementation(() => componentScope.stop())
    const reportingAttempts: ResourceReleaseAttempt[] = []
    const lifecycle = createApplicationLifecycle({
      app,
      mountTarget: "#app",
      reportClientEvent: vi.fn(),
      shutdownClientEventReporting: vi.fn(async (attempt) => {
        reportingAttempts.push(attempt)
      }),
    })
    await lifecycle.start()

    await expect(lifecycle.teardown()).resolves.toBeUndefined()
    expect(componentAttempts).toHaveLength(1)
    expect(reportingAttempts).toEqual(componentAttempts)
  })

  it("joins asynchronous component teardown created while unmounting", async () => {
    const componentCleanup = deferred<void>()
    const release = vi.fn(() => componentCleanup.promise)
    const componentScope = effectScope()
    componentScope.run(() => {
      observeAsynchronousScopeTeardown(
        "application async child teardown",
        release,
        vi.fn(),
      )
    })

    const { app, unmount } = createAppHarness()
    unmount.mockImplementation(() => componentScope.stop())
    const shutdownClientEventReporting = vi.fn(async () => {})
    const lifecycle = createApplicationLifecycle({
      app,
      mountTarget: "#app",
      reportClientEvent: vi.fn(),
      shutdownClientEventReporting,
    })
    await lifecycle.start()

    let settled = false
    const teardown = lifecycle.teardown().then(() => {
      settled = true
    })
    await vi.waitFor(() => expect(release).toHaveBeenCalledOnce())
    expect(unmount).toHaveBeenCalledOnce()
    expect(shutdownClientEventReporting).not.toHaveBeenCalled()
    expect(settled).toBe(false)

    componentCleanup.resolve()
    await expect(teardown).resolves.toBeUndefined()
    expect(shutdownClientEventReporting).toHaveBeenCalledOnce()
    expect(settled).toBe(true)
  })
})
