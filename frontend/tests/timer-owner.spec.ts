import { afterEach, describe, expect, it, vi } from "vitest"

import { createResourceOwner } from "@/lib/resource-owner"
import {
  createOwnedAnimationFrame,
  createOwnedInterval,
  createOwnedTimeout,
  type TimerLease,
} from "@/lib/timer-owner"

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

  const fire = (id: number) => {
    const timer = scheduled.get(id)
    if (!timer) throw new Error(`Timer ${id} is not scheduled`)
    scheduled.delete(id)
    timer.callback()
  }

  return { scheduled, setTimeout, clearTimeout, fire }
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe("owned timers", () => {
  it("retains the exact timeout ID after clear failure and blocks replacement until retry succeeds", () => {
    const timers = installTimerHarness()
    const cleanupError = new Error("clear failed")
    timers.clearTimeout.mockImplementationOnce(() => {
      throw cleanupError
    })
    const owner = createResourceOwner("replaceable timeout")
    let current: TimerLease | null = null

    const replace = () => {
      const previous = current
      if (previous) {
        previous.cancel()
        if (current === previous) current = null
      }
      current = createOwnedTimeout(owner, "timeout", () => undefined, 10)
    }

    replace()
    expect(() => replace()).toThrow(cleanupError)
    expect(timers.setTimeout).toHaveBeenCalledOnce()
    expect(timers.clearTimeout).toHaveBeenCalledWith(1)
    expect(current?.active).toBe(true)

    current?.cancel()
    current = null
    expect(timers.clearTimeout.mock.calls.map(([id]) => id)).toEqual([1, 1])

    replace()
    expect(timers.setTimeout).toHaveBeenCalledTimes(2)
    expect([...timers.scheduled.keys()]).toEqual([2])
    owner.dispose()
  })

  it("permanently stops a self-rescheduling interval when its callback throws", () => {
    const timers = installTimerHarness()
    const callbackError = new Error("tick failed")
    const owner = createResourceOwner("failing interval")
    const callback = vi.fn(() => {
      throw callbackError
    })

    createOwnedInterval(owner, "interval", callback, 25)
    expect([...timers.scheduled.keys()]).toEqual([1])

    expect(() => timers.fire(1)).toThrow(callbackError)
    expect(callback).toHaveBeenCalledOnce()
    expect(timers.setTimeout).toHaveBeenCalledOnce()
    expect(timers.scheduled.size).toBe(0)
    expect(owner.disposed).toBe(true)
    expect(() => createOwnedTimeout(owner, "late", () => undefined, 1)).toThrow(
      "Cannot acquire late after disposing failing interval",
    )
  })

  it("preserves a null callback failure while terminally disposing its owner", () => {
    const timers = installTimerHarness()
    const owner = createResourceOwner("null callback owner")
    createOwnedTimeout(owner, "null callback", () => { throw null }, 10)

    let callbackFailure: unknown = Symbol("no failure")
    try {
      timers.fire(1)
    } catch (failure) {
      callbackFailure = failure
    }

    expect(callbackFailure).toBeNull()
    expect(owner.disposed).toBe(true)
    expect(owner.settled).toBe(true)
    expect(timers.scheduled.size).toBe(0)
  })

  it("does not retry cleanup already attempted by the same timer callback", () => {
    const timers = installTimerHarness()
    const cleanupError = new Error("callback cleanup failed")
    const release = vi
      .fn<() => void>()
      .mockImplementationOnce(() => {
        throw cleanupError
      })
    const owner = createResourceOwner("timer callback owner")
    owner.claim("callback resource", release)
    createOwnedTimeout(owner, "callback", (attempt) => {
      owner.dispose(attempt)
    }, 10)

    expect(() => timers.fire(1)).toThrow(cleanupError)
    expect(release).toHaveBeenCalledOnce()
    expect(owner.settled).toBe(false)

    expect(() => owner.dispose({})).not.toThrow()
    expect(release).toHaveBeenCalledTimes(2)
    expect(owner.settled).toBe(true)
  })

  it("gates an unknown one-shot when timer setup mutates then throws", () => {
    const setupError = new Error("host timer failed after scheduling")
    let unknownCallback: (() => void) | null = null
    vi.spyOn(window, "setTimeout").mockImplementation(
      ((callback: TimerHandler) => {
        unknownCallback = callback as () => void
        throw setupError
      }) as typeof window.setTimeout,
    )
    const clearTimeout = vi.spyOn(globalThis, "clearTimeout")
    const owner = createResourceOwner("unknown timeout")
    const callback = vi.fn()

    expect(() => createOwnedTimeout(owner, "timeout", callback, 10)).toThrow(setupError)
    expect(clearTimeout).not.toHaveBeenCalled()

    ;(unknownCallback as (() => void) | null)?.()
    expect(callback).not.toHaveBeenCalled()
    expect(() => owner.dispose()).not.toThrow()
  })

  it("publishes a synchronously consumed timeout before running user code", () => {
    const clearTimeout = vi.spyOn(globalThis, "clearTimeout")
    vi.spyOn(window, "setTimeout").mockImplementation(
      ((callback: TimerHandler) => {
        ;(callback as () => void)()
        return 41
      }) as typeof window.setTimeout,
    )
    const owner = createResourceOwner("synchronous timeout")
    const callback = vi.fn(() => owner.dispose())

    const timeout = createOwnedTimeout(owner, "timeout", callback, 10)

    expect(callback).toHaveBeenCalledOnce()
    expect(timeout.active).toBe(false)
    expect(owner.disposed).toBe(true)
    expect(clearTimeout).not.toHaveBeenCalled()
  })

  it("permanently gates a cached animation-frame callback after cancel throws", () => {
    const callback = vi.fn()
    let cachedCallback: FrameRequestCallback | null = null
    const requestAnimationFrame = vi.spyOn(globalThis, "requestAnimationFrame")
      .mockImplementation((next) => {
        cachedCallback = next
        return 73
      })
    const cancelError = new Error("animation frame cancel failed")
    const cancelAnimationFrame = vi.spyOn(globalThis, "cancelAnimationFrame")
      .mockImplementationOnce(() => {
        cachedCallback = null
        throw cancelError
      })
    const owner = createResourceOwner("animation frame")
    const frame = createOwnedAnimationFrame(owner, "frame", callback)
    const staleCallback = cachedCallback

    expect(() => frame.cancel()).toThrow(cancelError)
    expect(frame.active).toBe(true)
    ;(staleCallback as FrameRequestCallback | null)?.(100)
    expect(callback).not.toHaveBeenCalled()

    expect(() => frame.cancel()).not.toThrow()
    expect(frame.active).toBe(false)
    expect(requestAnimationFrame).toHaveBeenCalledOnce()
    expect(cancelAnimationFrame.mock.calls.map(([id]) => id)).toEqual([73, 73])
    expect(() => owner.dispose()).not.toThrow()
  })

  it("lets an interval callback terminate the lifecycle without re-arming", () => {
    const timers = installTimerHarness()
    const owner = createResourceOwner("terminal interval")
    const callback = vi.fn(() => false)

    const interval = createOwnedInterval(owner, "interval", callback, 50)
    timers.fire(1)

    expect(callback).toHaveBeenCalledOnce()
    expect(interval.active).toBe(false)
    expect(timers.setTimeout).toHaveBeenCalledOnce()
    expect(timers.scheduled.size).toBe(0)
    expect(owner.settled).toBe(true)
  })

  it("owns only the currently scheduled one-shot across interval ticks", () => {
    const timers = installTimerHarness()
    const owner = createResourceOwner("steady interval")
    const callback = vi.fn()

    createOwnedInterval(owner, "interval", callback, 50)
    timers.fire(1)
    timers.fire(2)
    timers.fire(3)

    expect(callback).toHaveBeenCalledTimes(3)
    expect([...timers.scheduled.keys()]).toEqual([4])

    owner.dispose()
    expect(timers.clearTimeout.mock.calls.map(([id]) => id)).toEqual([4])
    expect(timers.scheduled.size).toBe(0)
  })

  it("does not overwrite a successor when an interval tick fires synchronously", () => {
    const timers = installTimerHarness()
    const originalSchedule = timers.setTimeout.getMockImplementation()!
    timers.setTimeout.mockImplementationOnce(
      ((callback: TimerHandler, delay?: number) => {
        const id = originalSchedule(callback, delay)
        timers.fire(id)
        return id
      }) as typeof window.setTimeout,
    )
    const owner = createResourceOwner("synchronous interval")
    const callback = vi.fn()

    const interval = createOwnedInterval(owner, "interval", callback, 50)

    expect(callback).toHaveBeenCalledOnce()
    expect(interval.active).toBe(true)
    expect([...timers.scheduled.keys()]).toEqual([2])
    interval.cancel()
    expect(timers.clearTimeout.mock.calls.map(([id]) => id)).toEqual([2])
    expect(timers.scheduled.size).toBe(0)
  })

  it("gates the unknown re-arm if a later interval schedule mutates then throws", () => {
    const timers = installTimerHarness()
    const rearmError = new Error("re-arm failed after scheduling")
    let unknownCallback: (() => void) | null = null
    timers.setTimeout.mockImplementationOnce(timers.setTimeout.getMockImplementation()!)
    timers.setTimeout.mockImplementationOnce(
      ((callback: TimerHandler) => {
        unknownCallback = callback as () => void
        throw rearmError
      }) as typeof window.setTimeout,
    )
    const owner = createResourceOwner("re-arm interval")
    const callback = vi.fn()

    createOwnedInterval(owner, "interval", callback, 5)
    expect(() => timers.fire(1)).toThrow(rearmError)
    expect(callback).toHaveBeenCalledOnce()
    expect(owner.disposed).toBe(true)

    ;(unknownCallback as (() => void) | null)?.()
    expect(callback).toHaveBeenCalledOnce()
    expect(timers.setTimeout).toHaveBeenCalledTimes(2)
  })
})
