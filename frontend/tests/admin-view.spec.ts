import { flushPromises, shallowMount } from "@vue/test-utils"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { getCurrentInstance } from "vue"

import type { KioskConfig, StopEntry } from "@/features/admin/api/admin"
import AdminView from "@/features/admin/AdminView.vue"
import {
  fetchAdminKiosk,
  fetchAdminStops,
  updateAdminKiosk,
} from "@/features/admin/api/admin"
import { settleRetainedAsynchronousTeardowns } from "@/lib/component-lifecycle"

vi.mock("@/features/admin/api/admin", async (importOriginal) => {
  const actual = await importOriginal<
    typeof import("@/features/admin/api/admin")
  >()
  return {
    ...actual,
    fetchAdminKiosk: vi.fn(),
    fetchAdminStops: vi.fn(),
    updateAdminKiosk: vi.fn(),
  }
})

type Deferred<T> = {
  promise: Promise<T>
  resolve: (value: T) => void
  reject: (reason?: unknown) => void
}

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, resolve, reject }
}

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

const currentConfig: KioskConfig = {
  stop_name: "current stop",
  direction: "回程",
  lat: 23.7,
  lng: 120.5,
}
const currentStop: StopEntry = {
  name: currentConfig.stop_name,
  lat: currentConfig.lat!,
  lng: currentConfig.lng!,
}

afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe("AdminView operation ownership", () => {
  beforeEach(() => {
    vi.mocked(fetchAdminKiosk).mockReset().mockResolvedValue(currentConfig)
    vi.mocked(fetchAdminStops).mockReset().mockResolvedValue([currentStop])
    vi.mocked(updateAdminKiosk).mockReset()
  })

  it("aborts a sibling request and keeps the group owned until every request settles", async () => {
    const primaryError = new Error("kiosk failed")
    const stopsRequest = deferred<StopEntry[]>()
    let stopsSignal: AbortSignal | undefined

    vi.mocked(fetchAdminKiosk).mockRejectedValue(primaryError)
    vi.mocked(fetchAdminStops).mockImplementation((signal) => {
      stopsSignal = signal
      return stopsRequest.promise
    })

    const wrapper = shallowMount(AdminView)

    await vi.waitFor(() => expect(stopsSignal?.aborted).toBe(true))
    expect(wrapper.text()).toContain("載入站牌清單")
    expect(wrapper.text()).not.toContain(primaryError.message)

    stopsRequest.reject(new DOMException("aborted", "AbortError"))
    await vi.waitFor(() => expect(wrapper.text()).toContain(primaryError.message))

    expect(fetchAdminKiosk).toHaveBeenCalledOnce()
    expect(fetchAdminStops).toHaveBeenCalledOnce()
    expect(fetchAdminKiosk).toHaveBeenCalledWith(stopsSignal)
    wrapper.unmount()
  })
  it("retains every independent initial-load failure", async () => {
    vi.mocked(fetchAdminKiosk).mockRejectedValue(new Error("kiosk failed"))
    vi.mocked(fetchAdminStops).mockRejectedValue(new Error("stops failed"))

    const wrapper = shallowMount(AdminView)

    await vi.waitFor(() => {
      expect(wrapper.text()).toContain("kiosk failed")
      expect(wrapper.text()).toContain("stops failed")
    })
    wrapper.unmount()
  })

  it("joins concurrent apply callers and snapshots the submitted selection", async () => {
    const update = deferred<KioskConfig>()
    vi.mocked(updateAdminKiosk).mockReturnValue(update.promise)
    const wrapper = shallowMount(AdminView)
    await vi.waitFor(() => expect(wrapper.text()).toContain("套用「current stop」回程"))

    const setupState = wrapper.vm.$.setupState as {
      handleApply: () => Promise<void>
      selectedDirection: "去程" | "回程" | null
    }
    const first = setupState.handleApply()
    const second = setupState.handleApply()
    setupState.selectedDirection = "去程"

    expect(second).toBe(first)
    await vi.waitFor(() => expect(updateAdminKiosk).toHaveBeenCalledOnce())
    expect(updateAdminKiosk).toHaveBeenCalledWith(
      { stop_name: "current stop", direction: "回程" },
      expect.any(AbortSignal),
    )

    update.resolve(currentConfig)
    await first
    wrapper.unmount()
  })

  it("keeps a disposed apply request owned until its physical promise settles", async () => {
    const update = deferred<KioskConfig>()
    let signal: AbortSignal | undefined
    vi.mocked(updateAdminKiosk).mockImplementation((_config, ownedSignal) => {
      signal = ownedSignal
      return update.promise
    })
    const wrapper = shallowMount(AdminView)
    await vi.waitFor(() => expect(wrapper.text()).toContain("套用「current stop」回程"))

    const setupState = wrapper.vm.$.setupState as {
      handleApply: () => Promise<void>
    }
    const operation = setupState.handleApply()
    await vi.waitFor(() => expect(signal).toBeDefined())
    wrapper.unmount()
    expect(signal?.aborted).toBe(true)

    let settled = false
    operation.then(() => {
      settled = true
    })
    await Promise.resolve()
    expect(settled).toBe(false)

    update.reject(new Error("late cancellation representation"))
    await operation
    expect(settled).toBe(true)
  })

  it("retries the exact apply-success timer before starting a replacement apply", async () => {
    const timers = installTimerHarness()
    const cleanupError = new Error("clear apply-success timer failed")
    timers.clearTimeout.mockImplementationOnce(() => {
      throw cleanupError
    })
    vi.mocked(updateAdminKiosk).mockResolvedValue(currentConfig)
    const wrapper = shallowMount(AdminView)
    await flushPromises()

    const setupState = wrapper.vm.$.setupState as {
      applySuccess: boolean
      handleApply: () => Promise<void>
    }
    await setupState.handleApply()
    await flushPromises()

    expect(setupState.applySuccess).toBe(true)
    expect(updateAdminKiosk).toHaveBeenCalledOnce()
    expect(timers.setTimeout).toHaveBeenCalledOnce()
    expect(timers.scheduled.get(1)?.delay).toBe(3000)
    const cachedFirstCallback = timers.scheduled.get(1)!.callback

    expect(() => setupState.handleApply()).toThrow(cleanupError)
    expect(updateAdminKiosk).toHaveBeenCalledOnce()
    expect(timers.setTimeout).toHaveBeenCalledOnce()
    expect(timers.clearTimeout.mock.calls.map(([id]) => id)).toEqual([1])

    await setupState.handleApply()
    await flushPromises()
    expect(updateAdminKiosk).toHaveBeenCalledTimes(2)
    expect(timers.setTimeout).toHaveBeenCalledTimes(2)
    expect(timers.clearTimeout.mock.calls.map(([id]) => id)).toEqual([1, 1])
    expect(setupState.applySuccess).toBe(true)

    cachedFirstCallback()
    expect(setupState.applySuccess).toBe(true)
    expect(timers.setTimeout).toHaveBeenCalledTimes(2)

    wrapper.unmount()
  })

  it("retains a mutate-then-throw abort claim and retries the same controller", async () => {
    const NativeAbortController = globalThis.AbortController
    const abortError = new Error("abort failed after mutating")
    const controllers: Array<{
      signal: AbortSignal
      abort: ReturnType<typeof vi.fn>
    }> = []

    class RetryableAbortController {
      readonly inner = new NativeAbortController()
      readonly signal = this.inner.signal
      readonly index = controllers.length
      readonly abort = vi.fn((reason?: unknown) => {
        this.inner.abort(reason)
        if (this.index === 1 && this.abort.mock.calls.length === 1) throw abortError
      })

      constructor() {
        controllers.push(this)
      }
    }

    vi.stubGlobal("AbortController", RetryableAbortController)
    const pendingRequest = (signal?: AbortSignal) => new Promise<never>((_resolve, reject) => {
      if (signal?.aborted) {
        reject(signal.reason)
        return
      }
      signal?.addEventListener("abort", () => reject(signal.reason), { once: true })
    })
    vi.mocked(fetchAdminKiosk).mockImplementation(pendingRequest)
    vi.mocked(fetchAdminStops).mockImplementation(pendingRequest)
    const handledErrors: unknown[] = []
    const wrapper = shallowMount(AdminView, {
      global: {
        config: {
          errorHandler: error => handledErrors.push(error),
        },
      },
    })
    await vi.waitFor(() => {
      expect(fetchAdminKiosk).toHaveBeenCalledOnce()
      expect(fetchAdminStops).toHaveBeenCalledOnce()
    })

    const settleTeardown = (wrapper.vm as unknown as {
      settleAdminViewTeardown: () => Promise<void>
    }).settleAdminViewTeardown
    const requestController = controllers[1]!
    wrapper.unmount()

    expect(requestController.signal.aborted).toBe(true)
    expect(requestController.abort).toHaveBeenCalledOnce()
    await vi.waitFor(() => expect(handledErrors).toHaveLength(1))
    expect(handledErrors[0]).toMatchObject({
      message: "Failed to release admin view resources",
    })

    await expect(settleTeardown()).resolves.toBeUndefined()
    expect(requestController.abort).toHaveBeenCalledTimes(2)
  })

  it("publishes request-controller ownership before constructor re-entry teardown", async () => {
    const NativeAbortController = globalThis.AbortController
    const controllers: ReentrantAbortController[] = []
    let stoppedScope = false

    class ReentrantAbortController {
      readonly inner = new NativeAbortController()
      readonly signal = this.inner.signal
      readonly abort = vi.fn((reason?: unknown) => {
        this.inner.abort(reason)
      })

      constructor() {
        controllers.push(this)
        if (controllers.length === 2) {
          const instance = getCurrentInstance()
          if (instance) {
            stoppedScope = true
            instance.scope.stop()
          }
        }
      }
    }

    vi.stubGlobal("AbortController", ReentrantAbortController)
    let setupFailure: unknown = null
    try {
      shallowMount(AdminView)
    } catch (error) {
      setupFailure = error
    }

    expect(controllers).toHaveLength(2)
    expect(stoppedScope).toBe(true)
    expect(setupFailure).toMatchObject({
      message: "Cannot finish acquiring admin initial-load abort controller after disposing admin view",
    })
    await expect(settleRetainedAsynchronousTeardowns()).resolves.toBeUndefined()
    expect(controllers[1]?.signal.aborted).toBe(true)
    expect(controllers[1]?.abort).toHaveBeenCalledOnce()
    expect(fetchAdminKiosk).not.toHaveBeenCalled()
    expect(fetchAdminStops).not.toHaveBeenCalled()
  })

  it("never enters the initial APIs when disposed before the published microtask runs", async () => {
    const wrapper = shallowMount(AdminView)
    const settleTeardown = (wrapper.vm as unknown as {
      settleAdminViewTeardown: () => Promise<void>
    }).settleAdminViewTeardown

    wrapper.unmount()
    await settleTeardown()
    await flushPromises()

    expect(fetchAdminKiosk).not.toHaveBeenCalled()
    expect(fetchAdminStops).not.toHaveBeenCalled()
  })

  it("retries the exact search-blur timer and gates its cached callback", async () => {
    const wrapper = shallowMount(AdminView)
    await flushPromises()
    const timers = installTimerHarness()
    const cleanupError = new Error("clear search-blur timer failed")
    timers.clearTimeout.mockImplementationOnce(() => {
      throw cleanupError
    })
    const setupState = wrapper.vm.$.setupState as {
      isSearchFocused: boolean
      handleSearchBlur: () => void
      handleSearchFocus: () => void
    }

    setupState.handleSearchFocus()
    setupState.handleSearchBlur()
    expect(timers.scheduled.get(1)?.delay).toBe(150)
    const cachedFirstCallback = timers.scheduled.get(1)!.callback

    expect(() => setupState.handleSearchBlur()).toThrow(cleanupError)
    expect(timers.setTimeout).toHaveBeenCalledOnce()
    expect(timers.clearTimeout.mock.calls.map(([id]) => id)).toEqual([1])

    setupState.handleSearchBlur()
    expect(timers.clearTimeout.mock.calls.map(([id]) => id)).toEqual([1, 1])
    expect(timers.setTimeout).toHaveBeenCalledTimes(2)
    expect(setupState.isSearchFocused).toBe(true)

    cachedFirstCallback()
    expect(setupState.isSearchFocused).toBe(true)
    expect(timers.setTimeout).toHaveBeenCalledTimes(2)

    wrapper.unmount()
  })

})
