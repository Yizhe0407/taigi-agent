import { mount } from "@vue/test-utils"
import { createApp, defineComponent, h, type ComputedRef } from "vue"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { useResolvedTheme } from "@/components/ui/map/composables/use-resolved-theme"
import type { Theme } from "@/components/ui/map/types"
import { createApplicationLifecycle } from "@/lib/application-lifecycle"
import { observeSynchronousScopeTeardown } from "@/lib/component-lifecycle"
import { createResourceOwner } from "@/lib/resource-owner"

const observerHarness = vi.hoisted(() => {
  type ObserverCallback = MutationCallback

  class Observer {
    static instances: Observer[] = []
    static observeError: Error | null = null
    static disconnectError: Error | null = null

    readonly callback: ObserverCallback
    readonly observe = vi.fn(() => {
      if (Observer.observeError) throw Observer.observeError
    })
    readonly disconnect = vi.fn(() => {
      if (
        Observer.disconnectError
        && this.disconnect.mock.calls.length === 1
      ) {
        throw Observer.disconnectError
      }
    })

    constructor(callback: ObserverCallback) {
      this.callback = callback
      Observer.instances.push(this)
    }
  }

  return { Observer }
})

type MediaHarness = {
  mediaQuery: MediaQueryList
  listeners: Set<EventListenerOrEventListenerObject>
  addEventListener: ReturnType<typeof vi.fn>
  removeEventListener: ReturnType<typeof vi.fn>
}

function installMediaHarness(options?: {
  addError?: Error
  removeError?: Error
}): MediaHarness {
  const listeners = new Set<EventListenerOrEventListenerObject>()
  const addEventListener = vi.fn(
    (_type: string, listener: EventListenerOrEventListenerObject) => {
      listeners.add(listener)
      if (options?.addError) throw options.addError
    },
  )
  const removeEventListener = vi.fn(
    (_type: string, listener: EventListenerOrEventListenerObject) => {
      listeners.delete(listener)
      if (
        options?.removeError
        && removeEventListener.mock.calls.length === 1
      ) {
        throw options.removeError
      }
    },
  )
  const mediaQuery = {
    matches: false,
    media: "(prefers-color-scheme: dark)",
    onchange: null,
    addEventListener,
    removeEventListener,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    dispatchEvent: vi.fn(() => true),
  } as unknown as MediaQueryList
  const matchMedia = vi.fn(() => mediaQuery)
  vi.stubGlobal("matchMedia", matchMedia)

  return { mediaQuery, listeners, addEventListener, removeEventListener }
}

function mountThemeHarness(errorHandler: (error: unknown) => void) {
  let resolvedTheme!: ComputedRef<Theme>
  let settleTeardown!: () => void

  const Harness = defineComponent({
    setup() {
      const owner = createResourceOwner("theme harness")
      resolvedTheme = useResolvedTheme(owner)
      settleTeardown = observeSynchronousScopeTeardown(
        "theme harness teardown",
        attempt => owner.dispose(attempt),
      )
      return () => h("span", resolvedTheme.value)
    },
  })

  const wrapper = mount(Harness, {
    global: { config: { errorHandler } },
  })
  return {
    wrapper,
    readTheme: () => resolvedTheme.value,
    settleTeardown: () => settleTeardown(),
  }
}

beforeEach(() => {
  document.documentElement.classList.remove("dark", "light")
  observerHarness.Observer.instances = []
  observerHarness.Observer.observeError = null
  observerHarness.Observer.disconnectError = null
  vi.stubGlobal("MutationObserver", observerHarness.Observer)
})

afterEach(() => {
  document.documentElement.classList.remove("dark", "light")
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe("useResolvedTheme ownership", () => {
  it("rolls back an observer registration that mutates then throws", () => {
    const setupError = new Error("observer registration failed after observing")
    observerHarness.Observer.observeError = setupError
    installMediaHarness()
    const handledErrors: unknown[] = []
    expect(() => mountThemeHarness(error => handledErrors.push(error))).toThrow(
      setupError,
    )
    const observer = observerHarness.Observer.instances[0]!

    expect(handledErrors).toEqual([setupError])
    expect(observer.observe).toHaveBeenCalledOnce()
    expect(observer.disconnect).toHaveBeenCalledOnce()

    const contains = vi.spyOn(document.documentElement.classList, "contains")
    document.documentElement.classList.add("dark")
    const checksBeforeCachedCallback = contains.mock.calls.length
    observer.callback([], observer as unknown as MutationObserver)
    expect(contains).toHaveBeenCalledTimes(checksBeforeCachedCallback)
  })

  it("rolls back the exact media listener when registration mutates then throws", () => {
    const setupError = new Error("media listener failed after registration")
    const media = installMediaHarness({ addError: setupError })
    const handledErrors: unknown[] = []
    expect(() => mountThemeHarness(error => handledErrors.push(error))).toThrow(
      setupError,
    )
    const observer = observerHarness.Observer.instances[0]!
    const registeredHandler = media.addEventListener.mock.calls[0]?.[1]

    expect(handledErrors).toEqual([setupError])
    expect(media.removeEventListener).toHaveBeenCalledWith(
      "change",
      registeredHandler,
    )
    expect(observer.disconnect).toHaveBeenCalledOnce()
    expect(media.listeners.size).toBe(0)

    const readMatches = vi.fn(() => true)
    const cachedEvent = {} as MediaQueryListEvent
    Object.defineProperty(cachedEvent, "matches", { get: readMatches })
    if (typeof registeredHandler === "function") registeredHandler(cachedEvent)
    expect(readMatches).not.toHaveBeenCalled()
  })

  it("keeps failed setup rollback debt fenced until application teardown", async () => {
    const setupError = new Error("media listener failed after registration")
    const cleanupError = new Error("media listener removal failed")
    const media = installMediaHarness({
      addError: setupError,
      removeError: cleanupError,
    })
    const Root = defineComponent({
      setup() {
        const owner = createResourceOwner("theme application harness")
        const resolvedTheme = useResolvedTheme(owner)
        observeSynchronousScopeTeardown(
          "theme application teardown",
          attempt => owner.dispose(attempt),
        )
        return () => h("span", resolvedTheme.value)
      },
    })
    const target = document.createElement("div")
    document.body.append(target)
    const app = createApp(Root)
    const handledErrors: unknown[] = []
    app.config.errorHandler = error => handledErrors.push(error)
    const lifecycle = createApplicationLifecycle({
      app,
      mountTarget: target,
      reportClientEvent: vi.fn(),
      shutdownClientEventReporting: vi.fn(async () => {}),
    })

    await lifecycle.start()
    const observer = observerHarness.Observer.instances[0]!
    expect(handledErrors).toHaveLength(1)
    expect(handledErrors[0]).toMatchObject({
      message: "Failed to acquire and roll back system theme listener",
    })
    expect(media.removeEventListener).toHaveBeenCalledOnce()
    expect(observer.disconnect).toHaveBeenCalledOnce()

    await lifecycle.teardown()
    expect(media.removeEventListener).toHaveBeenCalledTimes(2)
    expect(observer.disconnect).toHaveBeenCalledOnce()
    target.remove()
  })

  it("retains both cleanup debts, retries exact resources, and gates cached callbacks", () => {
    const disconnectError = new Error("observer disconnect failed after mutating")
    const removeError = new Error("media removal failed after mutating")
    observerHarness.Observer.disconnectError = disconnectError
    const media = installMediaHarness({ removeError })
    const handledErrors: unknown[] = []
    const harness = mountThemeHarness(error => handledErrors.push(error))
    const observer = observerHarness.Observer.instances[0]!
    const registeredHandler = media.addEventListener.mock.calls[0]?.[1]

    harness.wrapper.unmount()

    expect(handledErrors).toHaveLength(1)
    expect(handledErrors[0]).toMatchObject({
      message: "Failed to release theme harness",
      errors: [removeError, disconnectError],
    })
    expect(media.removeEventListener).toHaveBeenCalledTimes(1)
    expect(media.removeEventListener.mock.calls[0]?.[1]).toBe(registeredHandler)
    expect(observer.disconnect).toHaveBeenCalledOnce()

    expect(() => harness.settleTeardown()).not.toThrow()
    expect(media.removeEventListener).toHaveBeenCalledTimes(2)
    expect(media.removeEventListener.mock.calls[1]?.[1]).toBe(registeredHandler)
    expect(observer.disconnect).toHaveBeenCalledTimes(2)

    document.documentElement.classList.add("dark")
    observer.callback([], observer as unknown as MutationObserver)
    if (typeof registeredHandler === "function") {
      registeredHandler({ matches: true } as MediaQueryListEvent)
    }
    expect(harness.readTheme()).toBe("light")
  })

  it("terminally releases theme observers when a cached host getter throws null", () => {
    const media = installMediaHarness()
    const handledErrors: unknown[] = []
    const harness = mountThemeHarness(error => handledErrors.push(error))
    const observer = observerHarness.Observer.instances[0]!
    const registeredHandler = media.addEventListener.mock.calls[0]?.[1]
    const event = {} as MediaQueryListEvent
    Object.defineProperty(event, "matches", {
      get() {
        throw null
      },
    })

    expect(typeof registeredHandler).toBe("function")
    ;(registeredHandler as EventListener)(event)

    expect(handledErrors).toEqual([null])
    expect(media.removeEventListener).toHaveBeenCalledOnce()
    expect(observer.disconnect).toHaveBeenCalledOnce()

    const contains = vi.spyOn(document.documentElement.classList, "contains")
    observer.callback([], observer as unknown as MutationObserver)
    expect(contains).not.toHaveBeenCalled()
    harness.wrapper.unmount()
  })
})
