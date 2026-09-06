import { effectScope } from "vue"
import { afterEach, describe, expect, it, vi } from "vitest"

import {
  observeAsynchronousScopeTeardown,
  observeSynchronousScopeTeardown,
  consumeRetainedSynchronousTeardownFailure,
  runWithSynchronousScopeReleaseAttempt,
  settleRetainedAsynchronousTeardowns,
  settleRetainedSynchronousTeardowns,
} from "@/lib/component-lifecycle"
import type { ResourceReleaseAttempt } from "@/lib/resource-owner"

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

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe("component teardown retention", () => {
  it("retains a failed scope teardown for an explicit later attempt", () => {
    const cleanupError = new Error("scope cleanup failed")
    const attempts: ResourceReleaseAttempt[] = []
    const release = vi.fn((attempt: ResourceReleaseAttempt) => {
      attempts.push(attempt)
      if (release.mock.calls.length === 1) throw cleanupError
    })
    const reportError = vi.fn()
    vi.stubGlobal("reportError", reportError)
    const scope = effectScope()
    let settle!: () => void

    scope.run(() => {
      settle = observeSynchronousScopeTeardown("test scope teardown", release)
    })
    scope.stop()

    expect(reportError).toHaveBeenCalledWith(cleanupError)
    expect(consumeRetainedSynchronousTeardownFailure()).not.toBeNull()
    expect(release).toHaveBeenCalledOnce()

    expect(() => settle()).not.toThrow()
    expect(release).toHaveBeenCalledTimes(2)
    expect(attempts[1]).not.toBe(attempts[0])
    expect(consumeRetainedSynchronousTeardownFailure()).toBeNull()
  })

  it("retains a synchronous null failure until a distinct release attempt", () => {
    const release = vi
      .fn<(attempt: ResourceReleaseAttempt) => void>()
      .mockImplementationOnce(() => { throw null })
      .mockImplementationOnce(() => { throw null })
      .mockImplementationOnce(() => undefined)
    const reportError = vi.fn()
    vi.stubGlobal("reportError", reportError)
    const scope = effectScope()

    scope.run(() => {
      observeSynchronousScopeTeardown("null synchronous teardown", release)
    })
    scope.stop()
    expect(reportError).toHaveBeenCalledWith(null)
    expect(release).toHaveBeenCalledOnce()

    const attempt: ResourceReleaseAttempt = {}
    let firstFailure: unknown = Symbol("no failure")
    try {
      settleRetainedSynchronousTeardowns(attempt)
    } catch (failure) {
      firstFailure = failure
    }
    expect(firstFailure).toEqual(expect.objectContaining({ cause: null }))
    expect(release).toHaveBeenCalledTimes(2)

    expect(() => settleRetainedSynchronousTeardowns(attempt)).toThrow(
      "null synchronous teardown: null",
    )
    expect(release).toHaveBeenCalledTimes(2)

    expect(() => settleRetainedSynchronousTeardowns({})).not.toThrow()
    expect(release).toHaveBeenCalledTimes(3)
    expect(consumeRetainedSynchronousTeardownFailure()).toBeNull()
  })

  it("uses the next component disposal as a new attempt for older debt", () => {
    const cleanupError = new Error("first scope cleanup failed")
    const firstRelease = vi
      .fn<(attempt: ResourceReleaseAttempt) => void>()
      .mockImplementationOnce(() => {
        throw cleanupError
      })
    const secondRelease = vi.fn<(attempt: ResourceReleaseAttempt) => void>()
    const reportError = vi.fn()
    vi.stubGlobal("reportError", reportError)

    const firstScope = effectScope()
    firstScope.run(() => {
      observeSynchronousScopeTeardown("first scope teardown", firstRelease)
    })
    firstScope.stop()
    expect(firstRelease).toHaveBeenCalledOnce()

    const secondScope = effectScope()
    secondScope.run(() => {
      observeSynchronousScopeTeardown("second scope teardown", secondRelease)
    })
    secondScope.stop()

    expect(firstRelease).toHaveBeenCalledTimes(2)
    expect(secondRelease).toHaveBeenCalledOnce()
    expect(reportError).toHaveBeenCalledTimes(1)
    expect(consumeRetainedSynchronousTeardownFailure()).toBeNull()
  })

  it("propagates one retained retry attempt into nested scope disposal", () => {
    const nestedAttempts: ResourceReleaseAttempt[] = []
    const nestedScope = effectScope()
    nestedScope.run(() => {
      observeSynchronousScopeTeardown(
        "nested synchronous teardown",
        attempt => nestedAttempts.push(attempt),
      )
    })

    const cleanupError = new Error("outer teardown initially failed")
    const outerScope = effectScope()
    const outerRelease = vi
      .fn<(attempt: ResourceReleaseAttempt) => void>()
      .mockImplementationOnce(() => { throw cleanupError })
      .mockImplementationOnce(() => nestedScope.stop())
    vi.stubGlobal("reportError", vi.fn())
    outerScope.run(() => {
      observeSynchronousScopeTeardown("outer synchronous teardown", outerRelease)
    })
    outerScope.stop()

    const retryAttempt: ResourceReleaseAttempt = {}
    expect(() => settleRetainedSynchronousTeardowns(retryAttempt)).not.toThrow()
    expect(outerRelease).toHaveBeenCalledTimes(2)
    expect(nestedAttempts).toEqual([retryAttempt])
  })

  it("joins synchronous teardown that re-enters its own public cleanup", () => {
    const scope = effectScope()
    const teardown = vi.fn()
    let settle!: () => void
    let reenter = true

    scope.run(() => {
      settle = observeSynchronousScopeTeardown(
        "reentrant synchronous scope teardown",
        () => {
          teardown()
          if (reenter) {
            reenter = false
            settle()
          }
        },
      )
    })

    scope.stop()
    expect(teardown).toHaveBeenCalledOnce()
    expect(() => settle()).not.toThrow()
    expect(teardown).toHaveBeenCalledOnce()
  })

  it("retains a throwing reporter without interrupting later exact cleanups", () => {
    const cleanupError = new Error("persistent scope cleanup failed")
    const reportingError = new Error("global scope cleanup reporter failed")
    const firstRelease = vi.fn(() => {
      if (firstRelease.mock.calls.length <= 2) throw cleanupError
    })
    const secondRelease = vi.fn()
    const reportError = vi.fn(() => {
      throw reportingError
    })
    vi.stubGlobal("reportError", reportError)

    const firstScope = effectScope()
    firstScope.run(() => {
      observeSynchronousScopeTeardown("throwing reporter scope", firstRelease)
    })
    expect(() => firstScope.stop()).not.toThrow()

    const secondScope = effectScope()
    secondScope.run(() => {
      observeSynchronousScopeTeardown("later scope cleanup", secondRelease)
    })
    expect(() => secondScope.stop()).not.toThrow()

    expect(firstRelease).toHaveBeenCalledTimes(2)
    expect(secondRelease).toHaveBeenCalledOnce()
    expect(reportError).toHaveBeenCalledTimes(2)

    expect(() => settleRetainedSynchronousTeardowns({})).not.toThrow()
    expect(firstRelease).toHaveBeenCalledTimes(3)
    expect(reportError).toHaveBeenCalledTimes(2)

    const retainedFailure = consumeRetainedSynchronousTeardownFailure()
    expect(nestedErrorMessages(retainedFailure)).toEqual(expect.arrayContaining([
      expect.stringContaining("throwing reporter scope"),
      cleanupError.message,
      reportingError.message,
    ]))
    expect(consumeRetainedSynchronousTeardownFailure()).toBeNull()
  })

  it("does not retry the same retained teardown twice in one application attempt", () => {
    const cleanupError = new Error("persistent cleanup failure")
    const release = vi.fn(() => {
      throw cleanupError
    })
    const reportError = vi.fn()
    vi.stubGlobal("reportError", reportError)
    const scope = effectScope()

    scope.run(() => {
      observeSynchronousScopeTeardown("persistent scope teardown", release)
    })
    scope.stop()
    expect(release).toHaveBeenCalledOnce()

    const attempt: ResourceReleaseAttempt = {}
    expect(() => settleRetainedSynchronousTeardowns(attempt)).toThrow(
      "persistent scope teardown: persistent cleanup failure",
    )
    expect(() => settleRetainedSynchronousTeardowns(attempt)).toThrow(
      "persistent scope teardown: persistent cleanup failure",
    )
    expect(release).toHaveBeenCalledTimes(2)

    release.mockImplementation(() => undefined)
    expect(() => settleRetainedSynchronousTeardowns({})).not.toThrow()
    expect(release).toHaveBeenCalledTimes(3)
    expect(consumeRetainedSynchronousTeardownFailure()).toBeNull()
  })
})

describe("asynchronous component teardown retention", () => {
  it("retains a failed scope teardown for one explicit later attempt", async () => {
    const cleanupError = new Error("async scope cleanup failed")
    const attempts: ResourceReleaseAttempt[] = []
    const teardown = vi
      .fn<(attempt: ResourceReleaseAttempt) => Promise<void>>()
      .mockImplementationOnce(async (attempt) => {
        attempts.push(attempt)
        throw cleanupError
      })
      .mockImplementationOnce(async (attempt) => {
        attempts.push(attempt)
      })
    const reportFailure = vi.fn()
    const scope = effectScope()
    let settle!: () => Promise<void>

    scope.run(() => {
      settle = observeAsynchronousScopeTeardown(
        "async test scope teardown",
        teardown,
        reportFailure,
      )
    })
    scope.stop()

    await vi.waitFor(() => expect(reportFailure).toHaveBeenCalledWith(cleanupError))
    expect(teardown).toHaveBeenCalledOnce()

    await expect(settle()).resolves.toBeUndefined()
    expect(teardown).toHaveBeenCalledTimes(2)
    expect(attempts[1]).not.toBe(attempts[0])
    await expect(settleRetainedAsynchronousTeardowns()).resolves.toBeUndefined()
  })

  it("does not retry one failed teardown twice under the same release attempt", async () => {
    const cleanupError = new Error("persistent async cleanup failure")
    const teardown = vi.fn(async () => {
      throw cleanupError
    })
    const scope = effectScope()

    scope.run(() => {
      observeAsynchronousScopeTeardown(
        "persistent async scope teardown",
        teardown,
        vi.fn(),
      )
    })
    scope.stop()
    await vi.waitFor(() => expect(teardown).toHaveBeenCalledOnce())

    const attempt: ResourceReleaseAttempt = {}
    await expect(settleRetainedAsynchronousTeardowns(attempt)).rejects.toThrow(
      "persistent async scope teardown: persistent async cleanup failure",
    )
    await expect(settleRetainedAsynchronousTeardowns(attempt)).rejects.toThrow(
      "persistent async scope teardown: persistent async cleanup failure",
    )
    expect(teardown).toHaveBeenCalledTimes(2)

    teardown.mockResolvedValue(undefined)
    await expect(settleRetainedAsynchronousTeardowns({})).resolves.toBeUndefined()
    expect(teardown).toHaveBeenCalledTimes(3)
  })

  it("joins one in-flight physical teardown for every concurrent caller", async () => {
    const cleanup = deferred<void>()
    const teardown = vi.fn(() => cleanup.promise)
    const scope = effectScope()
    let settle!: () => Promise<void>

    scope.run(() => {
      settle = observeAsynchronousScopeTeardown(
        "joinable async scope teardown",
        teardown,
        vi.fn(),
      )
    })
    scope.stop()

    const explicitJoin = settle()
    const applicationJoin = settleRetainedAsynchronousTeardowns({})
    expect(teardown).toHaveBeenCalledOnce()

    cleanup.resolve()
    await expect(Promise.all([explicitJoin, applicationJoin])).resolves.toEqual([
      undefined,
      undefined,
    ])
    expect(teardown).toHaveBeenCalledOnce()
  })

  it("publishes asynchronous teardown before synchronous re-entry", async () => {
    const cleanup = deferred<void>()
    const scope = effectScope()
    let settle!: () => Promise<void>
    let reentrantJoin: Promise<void> | null = null
    let reenter = true
    const teardown = vi.fn(() => {
      if (reenter) {
        reenter = false
        reentrantJoin = settle()
      }
      return cleanup.promise
    })

    scope.run(() => {
      settle = observeAsynchronousScopeTeardown(
        "reentrant async scope teardown",
        teardown,
        vi.fn(),
      )
    })
    scope.stop()

    const explicitJoin = settle()
    expect(teardown).toHaveBeenCalledOnce()
    expect(reentrantJoin).toBe(explicitJoin)

    cleanup.resolve()
    await expect(explicitJoin).resolves.toBeUndefined()
    expect(teardown).toHaveBeenCalledOnce()
  })

  it("retains an asynchronous null rejection until a distinct attempt", async () => {
    const teardown = vi
      .fn<(attempt: ResourceReleaseAttempt) => Promise<void>>()
      .mockImplementationOnce(() => Promise.reject(null))
      .mockImplementationOnce(() => Promise.reject(null))
      .mockResolvedValueOnce(undefined)
    const reportFailure = vi.fn()
    const scope = effectScope()

    scope.run(() => {
      observeAsynchronousScopeTeardown(
        "null asynchronous teardown",
        teardown,
        reportFailure,
      )
    })
    scope.stop()
    await vi.waitFor(() => expect(reportFailure).toHaveBeenCalledWith(null))
    expect(teardown).toHaveBeenCalledOnce()

    const attempt: ResourceReleaseAttempt = {}
    let firstFailure: unknown = Symbol("no failure")
    try {
      await settleRetainedAsynchronousTeardowns(attempt)
    } catch (failure) {
      firstFailure = failure
    }
    expect(firstFailure).toEqual(expect.objectContaining({ cause: null }))
    expect(teardown).toHaveBeenCalledTimes(2)

    await expect(settleRetainedAsynchronousTeardowns(attempt)).rejects.toThrow(
      "null asynchronous teardown: null",
    )
    expect(teardown).toHaveBeenCalledTimes(2)

    await expect(settleRetainedAsynchronousTeardowns({})).resolves.toBeUndefined()
    expect(teardown).toHaveBeenCalledTimes(3)
  })

  it("does not let a late rejection retry the failed exact attempt", async () => {
    const cleanupError = new Error("late async cleanup failure")
    const cleanup = deferred<void>()
    const attempts: ResourceReleaseAttempt[] = []
    const teardown = vi.fn((attempt: ResourceReleaseAttempt) => {
      attempts.push(attempt)
      return cleanup.promise
    })
    const scope = effectScope()

    scope.run(() => {
      observeAsynchronousScopeTeardown(
        "late async scope teardown",
        teardown,
        vi.fn(),
      )
    })
    const attempt: ResourceReleaseAttempt = {}
    runWithSynchronousScopeReleaseAttempt(attempt, () => scope.stop())

    const firstJoin = settleRetainedAsynchronousTeardowns(attempt)
    const duplicateJoin = settleRetainedAsynchronousTeardowns(attempt)
    cleanup.reject(cleanupError)

    await expect(firstJoin).rejects.toThrow("late async cleanup failure")
    await expect(duplicateJoin).rejects.toThrow("late async cleanup failure")
    await expect(settleRetainedAsynchronousTeardowns(attempt)).rejects.toThrow(
      "late async cleanup failure",
    )
    expect(teardown).toHaveBeenCalledOnce()
    expect(attempts).toHaveLength(1)

    teardown.mockResolvedValue(undefined)
    await expect(settleRetainedAsynchronousTeardowns({})).resolves.toBeUndefined()
    expect(teardown).toHaveBeenCalledTimes(2)
  })

  it("retains a throwing failure reporter without creating an unhandled rejection", async () => {
    const cleanupError = new Error("reported async cleanup failed")
    const reportingError = new Error("async cleanup reporter failed")
    const teardown = vi
      .fn<() => Promise<void>>()
      .mockRejectedValueOnce(cleanupError)
      .mockResolvedValueOnce(undefined)
    const reportFailure = vi.fn(() => {
      throw reportingError
    })
    const unhandled = vi.fn()
    window.addEventListener("unhandledrejection", unhandled)
    const scope = effectScope()

    scope.run(() => {
      observeAsynchronousScopeTeardown(
        "throwing async cleanup reporter",
        teardown,
        reportFailure,
      )
    })
    scope.stop()

    await vi.waitFor(() => expect(reportFailure).toHaveBeenCalledWith(cleanupError))
    let retainedFailure: unknown
    try {
      await settleRetainedAsynchronousTeardowns({})
    } catch (error) {
      retainedFailure = error
    }

    expect(nestedErrorMessages(retainedFailure)).toEqual(expect.arrayContaining([
      expect.stringContaining("throwing async cleanup reporter"),
      cleanupError.message,
      reportingError.message,
    ]))
    expect(teardown).toHaveBeenCalledTimes(2)
    expect(reportFailure).toHaveBeenCalledOnce()
    expect(unhandled).not.toHaveBeenCalled()
    await expect(settleRetainedAsynchronousTeardowns({})).resolves.toBeUndefined()

    window.removeEventListener("unhandledrejection", unhandled)
  })
})
