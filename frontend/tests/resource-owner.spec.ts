import { describe, expect, it, vi } from "vitest"

import { createResourceOwner } from "@/lib/resource-owner"

describe("createResourceOwner", () => {
  it("releases resources in reverse order and permanently gates late callbacks", () => {
    const order: string[] = []
    const owner = createResourceOwner("test owner")
    const lateCallback = () => {
      if (!owner.signal.aborted) order.push("late")
    }

    owner.acquire(
      () => order.push("acquire-a"),
      () => order.push("release-a"),
    )
    owner.acquire(
      () => order.push("acquire-b"),
      () => order.push("release-b"),
    )

    lateCallback()
    owner.dispose()
    lateCallback()
    owner.dispose()

    expect(order).toEqual([
      "acquire-a",
      "acquire-b",
      "late",
      "release-b",
      "release-a",
    ])
    expect(() => owner.acquire(() => undefined, () => undefined)).toThrow(
      "Cannot acquire resource after disposing test owner",
    )
  })

  it("immediately rolls back a mutate-then-throw acquisition", () => {
    const setupError = new Error("setup failed")
    const releaseFirst = vi.fn()
    const releaseFailedAcquisition = vi.fn()
    const owner = createResourceOwner("failed setup")

    owner.acquire(() => undefined, releaseFirst)
    expect(() =>
      owner.acquire(() => {
        throw setupError
      }, releaseFailedAcquisition, "failing registration"),
    ).toThrow(setupError)

    expect(releaseFailedAcquisition).toHaveBeenCalledOnce()
    owner.dispose()
    expect(releaseFailedAcquisition).toHaveBeenCalledOnce()
    expect(releaseFirst).toHaveBeenCalledOnce()
  })

  it("aggregates rollback failure and retains that exact acquisition debt", () => {
    const setupError = new Error("setup failed")
    const cleanupError = new Error("cleanup failed")
    const release = vi
      .fn<() => void>()
      .mockImplementationOnce(() => {
        throw cleanupError
      })
    const owner = createResourceOwner("failed rollback")

    let failure: unknown
    try {
      owner.acquire(() => {
        throw setupError
      }, release, "retryable registration")
    } catch (error) {
      failure = error
    }

    expect(failure).toBeInstanceOf(AggregateError)
    expect((failure as AggregateError).errors).toEqual([setupError, cleanupError])
    expect(release).toHaveBeenCalledOnce()
    expect(owner.settled).toBe(false)

    expect(() => owner.dispose()).not.toThrow()
    expect(release).toHaveBeenCalledTimes(2)
    expect(owner.settled).toBe(true)
  })

  it("defers reentrant teardown until the acquired handle is published", () => {
    const owner = createResourceOwner("reentrant acquisition")
    const release = vi.fn<(value: string | undefined) => void>()
    let registered = false

    expect(() => owner.acquire(
      () => {
        owner.dispose()
        expect(owner.settled).toBe(false)
        registered = true
        return "exact handle"
      },
      (value) => {
        release(value)
        registered = false
      },
      "host registration",
    )).toThrow(
      "Cannot finish acquiring host registration after disposing reentrant acquisition",
    )

    expect(release).toHaveBeenCalledWith("exact handle")
    expect(registered).toBe(false)
    expect(owner.settled).toBe(true)
    expect(() => owner.dispose()).not.toThrow()
    expect(release).toHaveBeenCalledOnce()
  })

  it("attempts every release and retries only exact cleanup debt", () => {
    const firstRelease = vi.fn()
    const retryableError = new Error("release failed")
    const retryableRelease = vi
      .fn<() => void>()
      .mockImplementationOnce(() => {
        throw retryableError
      })
    const lastRelease = vi.fn()
    const owner = createResourceOwner("retryable owner")

    owner.acquire(() => undefined, firstRelease)
    owner.acquire(() => undefined, retryableRelease)
    owner.acquire(() => undefined, lastRelease)

    expect(() => owner.dispose()).toThrow(retryableError)
    expect(lastRelease).toHaveBeenCalledOnce()
    expect(retryableRelease).toHaveBeenCalledOnce()
    expect(firstRelease).toHaveBeenCalledOnce()

    expect(() => owner.dispose()).not.toThrow()
    expect(lastRelease).toHaveBeenCalledOnce()
    expect(retryableRelease).toHaveBeenCalledTimes(2)
    expect(firstRelease).toHaveBeenCalledOnce()
  })

  it("fences one higher-level cleanup attempt without retaining unrelated resources", () => {
    const cleanupError = new Error("release failed")
    const retryableRelease = vi
      .fn<() => void>()
      .mockImplementationOnce(() => {
        throw cleanupError
      })
    const otherRelease = vi.fn()
    const owner = createResourceOwner("attempt-fenced owner")
    const attempt = {}
    const retryable = owner.claim("retryable resource", retryableRelease)
    owner.claim("other resource", otherRelease)

    expect(() => retryable.release(attempt)).toThrow(cleanupError)
    expect(() => owner.dispose(attempt)).not.toThrow()
    expect(retryableRelease).toHaveBeenCalledOnce()
    expect(otherRelease).toHaveBeenCalledOnce()
    expect(owner.settled).toBe(false)

    expect(() => owner.dispose({})).not.toThrow()
    expect(retryableRelease).toHaveBeenCalledTimes(2)
    expect(otherRelease).toHaveBeenCalledOnce()
    expect(owner.settled).toBe(true)
  })

  it("does not repeat failed acquisition rollback in the same cleanup attempt", () => {
    const setupError = new Error("registration failed")
    const cleanupError = new Error("registration rollback failed")
    const release = vi
      .fn<() => void>()
      .mockImplementationOnce(() => {
        throw cleanupError
      })
    const owner = createResourceOwner("attempt-fenced rollback")
    const attempt = {}

    expect(() => owner.acquire(
      () => { throw setupError },
      release,
      "registration",
      attempt,
    )).toThrow("Failed to acquire and roll back registration")
    expect(() => owner.dispose(attempt)).not.toThrow()
    expect(release).toHaveBeenCalledOnce()
    expect(owner.settled).toBe(false)

    expect(() => owner.dispose({})).not.toThrow()
    expect(release).toHaveBeenCalledTimes(2)
    expect(owner.settled).toBe(true)
  })

  it("joins reentrant disposal without replaying a failed exact release", () => {
    const cleanupError = new Error("release failed after reentrant disposal")
    const owner = createResourceOwner("reentrant release owner")
    const release = vi
      .fn<() => void>()
      .mockImplementationOnce(() => {
        owner.dispose()
        throw cleanupError
      })
    owner.claim("reentrant resource", release)

    expect(() => owner.dispose()).toThrow(cleanupError)
    expect(release).toHaveBeenCalledOnce()
    expect(owner.settled).toBe(false)

    expect(() => owner.dispose()).not.toThrow()
    expect(release).toHaveBeenCalledTimes(2)
    expect(owner.settled).toBe(true)
  })

  it("transfers a claim without releasing the transferred resource", () => {
    const release = vi.fn()
    const owner = createResourceOwner("transfer owner")
    const claim = owner.claim("transferred resource", release)

    claim.transfer()
    claim.transfer()
    owner.dispose()

    expect(claim.active).toBe(false)
    expect(release).not.toHaveBeenCalled()
  })

  it("close permanently gates acquisition while dispose still releases and retries old claims", () => {
    const cleanupError = new Error("cleanup failed")
    const release = vi
      .fn<() => void>()
      .mockImplementationOnce(() => {
        throw cleanupError
      })
    const owner = createResourceOwner("closed owner")
    owner.claim("existing resource", release)

    owner.close()

    expect(owner.disposed).toBe(true)
    expect(owner.signal.aborted).toBe(true)
    expect(release).not.toHaveBeenCalled()
    expect(() => owner.claim("late resource", () => undefined)).toThrow(
      "Cannot acquire late resource after disposing closed owner",
    )
    expect(() => owner.dispose()).toThrow(cleanupError)
    expect(() => owner.dispose()).not.toThrow()
    expect(release).toHaveBeenCalledTimes(2)
  })

  it("retains AbortController mutate-then-throw debt for a new exact attempt", () => {
    const abortFailure = new Error("owner abort failed after mutation")
    const abort = vi.fn<AbortController["abort"]>()
    class ThrowingAbortController extends AbortController {
      override abort(reason?: unknown): void {
        super.abort(reason)
        abort(reason)
        if (abort.mock.calls.length === 1) throw abortFailure
      }
    }
    vi.stubGlobal("AbortController", ThrowingAbortController)
    const owner = createResourceOwner("retryable close owner")
    const firstAttempt = {}

    expect(() => owner.dispose(firstAttempt)).toThrow(abortFailure)
    expect(owner.disposed).toBe(true)
    expect(owner.signal.aborted).toBe(true)
    expect(owner.settled).toBe(false)
    expect(abort).toHaveBeenCalledOnce()

    expect(() => owner.dispose(firstAttempt)).not.toThrow()
    expect(owner.settled).toBe(false)
    expect(abort).toHaveBeenCalledOnce()

    expect(() => owner.dispose({})).not.toThrow()
    expect(owner.settled).toBe(true)
    expect(abort).toHaveBeenCalledTimes(2)
  })
})
