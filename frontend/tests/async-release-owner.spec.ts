import { describe, expect, it, vi } from "vitest"

import { createAsyncReleaseOwner } from "@/lib/async-release-owner"

type Deferred<T> = {
  promise: Promise<T>
  resolve: (value: T) => void
  reject: (reason: unknown) => void
}

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<T>((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, resolve, reject }
}

describe("createAsyncReleaseOwner", () => {
  it("publishes and joins one physical release before user code settles", async () => {
    const physical = deferred<void>()
    const release = vi.fn(() => physical.promise)
    const owner = createAsyncReleaseOwner("test resources")
    const action = owner.claim("pending resource", release)
    const attempt = {}

    const first = owner.start(action, attempt)
    const second = owner.start(action, attempt)

    expect(second).toBe(first)
    expect(action.operation).toBe(first)
    expect(release).toHaveBeenCalledOnce()
    expect(owner.settled).toBe(false)

    physical.resolve()
    await first

    expect(action.active).toBe(false)
    expect(owner.settled).toBe(true)
  })

  it("never rejects a callback-started operation and retries only failed debt", async () => {
    const cleanupError = new Error("cleanup failed")
    const failedRelease = vi
      .fn<() => Promise<void>>()
      .mockRejectedValueOnce(cleanupError)
      .mockResolvedValue(undefined)
    const successfulRelease = vi.fn(async () => undefined)
    const owner = createAsyncReleaseOwner("retryable resources")
    const failedAction = owner.claim("failed resource", failedRelease)
    owner.claim("successful resource", successfulRelease)
    const firstAttempt = {}

    await expect(owner.start(failedAction, firstAttempt)).resolves.toBeUndefined()
    expect(failedAction.failure).toBe(cleanupError)
    await expect(owner.settle(firstAttempt)).rejects.toThrow("cleanup failed")
    expect(failedRelease).toHaveBeenCalledOnce()
    expect(successfulRelease).toHaveBeenCalledOnce()

    await expect(owner.settle({})).resolves.toBeUndefined()
    expect(failedRelease).toHaveBeenCalledTimes(2)
    expect(successfulRelease).toHaveBeenCalledOnce()
    expect(owner.settled).toBe(true)
  })

  it("retains an undefined rejection as failed debt", async () => {
    const release = vi
      .fn<() => Promise<void>>()
      .mockImplementationOnce(() => Promise.reject(undefined))
      .mockResolvedValueOnce(undefined)
    const owner = createAsyncReleaseOwner("undefined rejection resources")
    const action = owner.claim("undefined rejection", release)
    const firstAttempt = {}

    await expect(owner.start(action, firstAttempt)).resolves.toBeUndefined()
    expect(action.active).toBe(true)
    expect(action.failed).toBe(true)
    expect(action.failure).toBeUndefined()
    expect(owner.settled).toBe(false)
    await expect(owner.settle(firstAttempt)).rejects.toThrow(
      "undefined rejection: undefined",
    )
    expect(release).toHaveBeenCalledOnce()

    await expect(owner.settle({})).resolves.toBeUndefined()
    expect(release).toHaveBeenCalledTimes(2)
    expect(owner.settled).toBe(true)
  })

  it("retains a null rejection as exact failed debt", async () => {
    const release = vi
      .fn<() => Promise<void>>()
      .mockImplementationOnce(() => Promise.reject(null))
      .mockResolvedValueOnce(undefined)
    const owner = createAsyncReleaseOwner("null rejection resources")
    const action = owner.claim("null rejection", release)
    const firstAttempt = {}

    await expect(owner.start(action, firstAttempt)).resolves.toBeUndefined()
    expect(action.active).toBe(true)
    expect(action.failed).toBe(true)
    expect(action.failure).toBeNull()
    expect(owner.settled).toBe(false)

    let firstFailure: unknown = Symbol("no failure")
    try {
      await owner.settle(firstAttempt)
    } catch (failure) {
      firstFailure = failure
    }
    expect(firstFailure).toEqual(expect.objectContaining({ cause: null }))
    expect(release).toHaveBeenCalledOnce()

    await expect(owner.settle({})).resolves.toBeUndefined()
    expect(release).toHaveBeenCalledTimes(2)
    expect(owner.settled).toBe(true)
  })

  it("joins an older in-flight attempt before retrying it under a new attempt", async () => {
    const firstPhysical = deferred<void>()
    const cleanupError = new Error("first attempt failed")
    const release = vi
      .fn<() => Promise<void>>()
      .mockImplementationOnce(() => firstPhysical.promise)
      .mockResolvedValueOnce(undefined)
    const owner = createAsyncReleaseOwner("queued retry resources")
    const action = owner.claim("retryable resource", release)

    owner.start(action, {})
    const retry = owner.settle({})
    expect(release).toHaveBeenCalledOnce()

    firstPhysical.reject(cleanupError)
    await expect(retry).resolves.toBeUndefined()
    expect(release).toHaveBeenCalledTimes(2)
    expect(owner.settled).toBe(true)
  })

  it("retains a mutate-then-throw release for a distinct exact attempt", async () => {
    const cleanupError = new Error("release failed after mutation")
    const release = vi
      .fn<() => void>()
      .mockImplementationOnce(() => {
        throw cleanupError
      })
    const owner = createAsyncReleaseOwner("synchronous resources")
    owner.claim("mutating resource", release)
    const attempt = {}

    await expect(owner.settle(attempt)).rejects.toThrow(cleanupError.message)
    await expect(owner.settle(attempt)).rejects.toThrow(cleanupError.message)
    expect(release).toHaveBeenCalledOnce()

    await expect(owner.settle({})).resolves.toBeUndefined()
    expect(release).toHaveBeenCalledTimes(2)
  })
})
