import { ref } from "vue"

import {
  currentSynchronousScopeReleaseAttempt,
  observeSynchronousScopeTeardown,
  runWithSynchronousScopeReleaseAttempt,
} from "@/lib/component-lifecycle"
import {
  createResourceOwner,
  type ResourceReleaseAttempt,
} from "@/lib/resource-owner"
import {
  createOwnedInterval,
  createOwnedTimeout,
  type TimerLease,
} from "@/lib/timer-owner"

// 45s of no activity -> warning card w/ 15s countdown -> zero closes. Any
// voice event or touch (see markActivity) cancels and returns to the prior
// state. Replaces the old blind 60s auto-close.
const IDLE_WARN_AFTER_MS = 45_000
const IDLE_WARN_COUNTDOWN_S = 15

function throwFailures(failures: unknown[], message: string): void {
  if (failures.length === 1) throw failures[0]
  if (failures.length > 1) throw new AggregateError(failures, message)
}

/**
 * PiP idle-close timer: 45s of no activity shows a 15s-countdown warning
 * card, then `onExpire` (closes the PiP). Any caller-observed activity calls
 * `markActivity`/`resetIdleTimer` to push the clock back out.
 */
export function usePipIdleTimer(onExpire: () => void) {
  const showIdleWarning = ref(false)
  const idleWarnSecondsLeft = ref(IDLE_WARN_COUNTDOWN_S)
  const owner = createResourceOwner("PiP idle timer")
  let idleTimer: TimerLease | null = null
  let idleWarnInterval: TimerLease | null = null
  let idleTimerGeneration = 0
  let idleWarnGeneration = 0

  function runOperation<T>(
    operation: (attempt: ResourceReleaseAttempt) => T,
  ): T {
    const attempt = currentSynchronousScopeReleaseAttempt() ?? {}
    return runWithSynchronousScopeReleaseAttempt(
      attempt,
      () => operation(attempt),
    )
  }

  function closeAfterSetupFailure(
    setupError: unknown,
    attempt: ResourceReleaseAttempt,
    lifecycle: string,
  ): never {
    try {
      owner.dispose(attempt)
    } catch (cleanupError) {
      throw new AggregateError(
        [setupError, cleanupError],
        `Failed to initialize and close ${lifecycle}`,
      )
    }
    throw setupError
  }

  function clearIdleTimer(attempt: ResourceReleaseAttempt): void {
    idleTimerGeneration++
    const current = idleTimer
    if (!current) return
    current.cancel(attempt)
    if (!current.active && idleTimer === current) idleTimer = null
  }

  function clearIdleWarnInterval(attempt: ResourceReleaseAttempt): void {
    idleWarnGeneration++
    const current = idleWarnInterval
    if (!current) return
    current.cancel(attempt)
    if (!current.active && idleWarnInterval === current) idleWarnInterval = null
  }

  function pauseIdleTrackingWithAttempt(attempt: ResourceReleaseAttempt): void {
    showIdleWarning.value = false
    const failures: unknown[] = []
    try {
      clearIdleTimer(attempt)
    } catch (error) {
      failures.push(error)
    }
    try {
      clearIdleWarnInterval(attempt)
    } catch (error) {
      failures.push(error)
    }
    throwFailures(failures, "Failed to pause PiP idle tracking")
  }

  function pauseIdleTracking(): void {
    runOperation(pauseIdleTrackingWithAttempt)
  }

  function startIdleWarning(attempt: ResourceReleaseAttempt): void {
    if (owner.disposed) return
    clearIdleWarnInterval(attempt)
    showIdleWarning.value = true
    idleWarnSecondsLeft.value = IDLE_WARN_COUNTDOWN_S

    const generation = ++idleWarnGeneration
    let next: TimerLease
    try {
      next = createOwnedInterval(
        owner,
        "PiP idle warning interval",
        callbackAttempt => {
          if (owner.disposed || idleWarnGeneration !== generation) return false
          idleWarnSecondsLeft.value -= 1
          if (idleWarnSecondsLeft.value > 0) return true

          const failures: unknown[] = []
          try {
            pauseIdleTrackingWithAttempt(callbackAttempt)
          } catch (error) {
            failures.push(error)
          }
          if (!owner.disposed) {
            try {
              runWithSynchronousScopeReleaseAttempt(callbackAttempt, onExpire)
            } catch (error) {
              failures.push(error)
            }
          }
          throwFailures(failures, "PiP idle expiry failed")
          return false
        },
        1000,
        attempt,
      )
    } catch (setupError) {
      if (idleWarnGeneration === generation) idleWarnGeneration++
      return closeAfterSetupFailure(
        setupError,
        attempt,
        "PiP idle warning interval",
      )
    }

    if (
      !owner.disposed
      && idleWarnGeneration === generation
      && next.active
    ) {
      idleWarnInterval = next
    } else if (next.active) {
      next.cancel(attempt)
    }
  }

  function resetIdleTimerWithAttempt(attempt: ResourceReleaseAttempt): void {
    pauseIdleTrackingWithAttempt(attempt)
    if (owner.disposed) return

    const generation = ++idleTimerGeneration
    let next: TimerLease
    try {
      next = createOwnedTimeout(
        owner,
        "PiP idle deadline",
        callbackAttempt => {
          if (owner.disposed || idleTimerGeneration !== generation) return
          idleTimerGeneration++
          const current = idleTimer
          if (current && !current.active) idleTimer = null
          startIdleWarning(callbackAttempt)
        },
        IDLE_WARN_AFTER_MS,
        attempt,
      )
    } catch (setupError) {
      if (idleTimerGeneration === generation) idleTimerGeneration++
      return closeAfterSetupFailure(setupError, attempt, "PiP idle deadline")
    }

    if (
      !owner.disposed
      && idleTimerGeneration === generation
      && next.active
    ) {
      idleTimer = next
    }
  }

  function resetIdleTimer(): void {
    runOperation(resetIdleTimerWithAttempt)
  }

  /** Any voice event or touch resets the 45s idle clock and cancels the warning card. */
  function markActivity(): void {
    runOperation(resetIdleTimerWithAttempt)
  }

  observeSynchronousScopeTeardown("PiP idle timer teardown", attempt => {
    idleTimerGeneration++
    idleWarnGeneration++
    showIdleWarning.value = false
    owner.dispose(attempt)
    if (!owner.settled) {
      throw new Error("PiP idle timer cleanup remains unresolved")
    }
    idleTimer = null
    idleWarnInterval = null
  })

  return {
    showIdleWarning,
    idleWarnSecondsLeft,
    resetIdleTimer,
    pauseIdleTracking,
    markActivity,
  }
}
