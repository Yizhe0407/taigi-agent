import type {
  ResourceClaim,
  ResourceOwner,
  ResourceReleaseAttempt,
} from "@/lib/resource-owner"

export type TimerLease = {
  readonly active: boolean
  cancel(attempt?: ResourceReleaseAttempt): void
}

function throwOneShotFailures(
  lifecycle: string,
  failures: unknown[],
): void {
  if (failures.length === 1) throw failures[0]
  if (failures.length > 1) {
    throw new AggregateError(
      failures,
      `${lifecycle} callback and cleanup failed`,
    )
  }
}

function createOwnedOneShot(
  owner: ResourceOwner,
  lifecycle: string,
  schedule: (callback: (timestamp: number) => void) => number,
  cancelScheduled: (id: number) => void,
  callback: (
    timestamp: number,
    attempt: ResourceReleaseAttempt,
  ) => void,
  acquisitionAttempt: ResourceReleaseAttempt,
): TimerLease {
  let callbackEnabled = true
  let acquisitionComplete = false
  let callbackPending = false
  let pendingTimestamp = 0
  let claim: ResourceClaim | null = null

  const run = (timestamp: number) => {
    if (!callbackEnabled) return
    if (!acquisitionComplete) {
      callbackPending = true
      pendingTimestamp = timestamp
      return
    }

    const ownedClaim = claim
    if (!ownedClaim?.active) return

    // A one-shot is physically consumed before user code can re-enter teardown.
    ownedClaim.transfer()
    if (owner.disposed) return

    const attempt: ResourceReleaseAttempt = {}
    const failures: unknown[] = []
    try {
      callback(timestamp, attempt)
    } catch (error) {
      failures.push(error)
      // If user code synchronously entered a terminal cleanup, that operation
      // owns any exact debt. Do not mint a second attempt from the returning
      // timer callback and retry it immediately.
      if (!owner.disposed) {
        try {
          owner.dispose(attempt)
        } catch (ownerCleanupError) {
          failures.push(ownerCleanupError)
        }
      }
    }
    throwOneShotFailures(lifecycle, failures)
  }

  const acquisition = owner.acquire(
    () => schedule(run),
    (id) => {
      // Gate even an unknowable handle when a host schedules and then throws.
      callbackEnabled = false
      if (id !== undefined) cancelScheduled(id)
    },
    lifecycle,
    acquisitionAttempt,
  )
  claim = acquisition
  acquisitionComplete = true
  if (callbackPending) run(pendingTimestamp)

  return {
    get active() {
      return acquisition.active
    },
    cancel(attempt) {
      acquisition.release(attempt)
    },
  }
}

/** Publish and transactionally acquire an exact one-shot browser timer. */
export function createOwnedTimeout(
  owner: ResourceOwner,
  lifecycle: string,
  callback: (attempt: ResourceReleaseAttempt) => void,
  delayMs: number,
  acquisitionAttempt: ResourceReleaseAttempt = {},
): TimerLease {
  return createOwnedOneShot(
    owner,
    lifecycle,
    run => window.setTimeout(() => run(0), delayMs),
    id => clearTimeout(id),
    (_timestamp, attempt) => callback(attempt),
    acquisitionAttempt,
  )
}

/** Publish and transactionally acquire an exact one-shot animation frame. */
export function createOwnedAnimationFrame(
  owner: ResourceOwner,
  lifecycle: string,
  callback: (
    timestamp: number,
    attempt: ResourceReleaseAttempt,
  ) => void,
  acquisitionAttempt: ResourceReleaseAttempt = {},
): TimerLease {
  return createOwnedOneShot(
    owner,
    lifecycle,
    run => requestAnimationFrame(run),
    id => cancelAnimationFrame(id),
    callback,
    acquisitionAttempt,
  )
}

/**
 * Owned repeating timer implemented as a chain of independently owned
 * one-shots. Every re-arm is a new transactional acquisition, so teardown that
 * re-enters a host timer call cannot orphan the newly returned timer ID.
 */
export function createOwnedInterval(
  owner: ResourceOwner,
  lifecycle: string,
  callback: (attempt: ResourceReleaseAttempt) => boolean | void,
  delayMs: number,
  setupAttempt: ResourceReleaseAttempt = {},
): TimerLease {
  let cancelRequested = false
  let currentTimer: TimerLease | null = null
  let scheduledGeneration = 0
  let currentGeneration = 0
  const lifecycleClaim = owner.claim(lifecycle, () => {
    cancelRequested = true
  })

  const cancel = (attempt?: ResourceReleaseAttempt) => {
    cancelRequested = true
    const timer = currentTimer
    if (timer) {
      timer.cancel(attempt)
      if (currentTimer === timer) currentTimer = null
    }
    lifecycleClaim.release(attempt)
  }

  const scheduleNext = (acquisitionAttempt: ResourceReleaseAttempt) => {
    const generation = ++scheduledGeneration
    currentGeneration = generation
    const next = createOwnedTimeout(owner, `${lifecycle} tick`, (attempt) => {
      if (currentGeneration === generation) currentTimer = null
      if (cancelRequested || owner.disposed) return

      const keepRunning = callback(attempt)
      if (keepRunning === false) {
        cancelRequested = true
        lifecycleClaim.release(attempt)
        return
      }
      if (cancelRequested || owner.disposed) return
      scheduleNext(attempt)
    }, delayMs, acquisitionAttempt)
    if (
      currentGeneration === generation
      && !cancelRequested
      && !owner.disposed
      && next.active
    ) {
      currentTimer = next
    }
  }

  try {
    scheduleNext(setupAttempt)
  } catch (setupError) {
    try {
      cancel(setupAttempt)
    } catch (cleanupError) {
      throw new AggregateError(
        [setupError, cleanupError],
        `Failed to initialize and roll back ${lifecycle}`,
      )
    }
    throw setupError
  }

  return {
    get active() {
      return lifecycleClaim.active && !cancelRequested
    },
    cancel,
  }
}
