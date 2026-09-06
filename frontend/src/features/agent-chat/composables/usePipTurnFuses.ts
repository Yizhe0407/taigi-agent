import { watch, type Ref } from "vue"

import {
  currentSynchronousScopeReleaseAttempt,
  observeSynchronousScopeTeardown,
  runWithSynchronousScopeReleaseAttempt,
} from "@/lib/component-lifecycle"
import {
  createResourceOwner,
  type ResourceReleaseAttempt,
} from "@/lib/resource-owner"
import { createOwnedTimeout, type TimerLease } from "@/lib/timer-owner"

import type { ConversationState } from "../types"

/** Generic single-shot timer: `start()` (re)arms it, `clear()` disarms it. */
function useFuseTimer(ms: number, onExpire: () => void) {
  const owner = createResourceOwner("PiP fuse timer")
  let timer: TimerLease | null = null
  let timerGeneration = 0

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
  ): never {
    try {
      owner.dispose(attempt)
    } catch (cleanupError) {
      throw new AggregateError(
        [setupError, cleanupError],
        "Failed to initialize and close PiP fuse timer",
      )
    }
    throw setupError
  }

  function clearTimer(attempt: ResourceReleaseAttempt): void {
    timerGeneration++
    const current = timer
    if (!current) return
    current.cancel(attempt)
    if (!current.active && timer === current) timer = null
  }

  function clear(): void {
    runOperation(clearTimer)
  }

  function startTimer(attempt: ResourceReleaseAttempt): void {
    if (owner.disposed) return
    clearTimer(attempt)

    const generation = ++timerGeneration
    let next: TimerLease
    try {
      next = createOwnedTimeout(
        owner,
        "PiP fuse timeout",
        callbackAttempt => {
          if (owner.disposed || timerGeneration !== generation) return
          timerGeneration++
          const current = timer
          if (current && !current.active) timer = null
          runWithSynchronousScopeReleaseAttempt(callbackAttempt, onExpire)
        },
        ms,
        attempt,
      )
    } catch (setupError) {
      return closeAfterSetupFailure(setupError, attempt)
    }

    if (
      !owner.disposed
      && timerGeneration === generation
      && next.active
    ) {
      timer = next
    }
  }

  function start(): void {
    runOperation(startTimer)
  }

  observeSynchronousScopeTeardown("PiP fuse timer teardown", attempt => {
    timerGeneration++
    owner.dispose(attempt)
    if (!owner.settled) {
      throw new Error("PiP fuse timer cleanup remains unresolved")
    }
    timer = null
  })

  return { start, clear }
}

/**
 * 30s safety fuse: total-failure fallback if neither a reply nor a cancel
 * ever arrives for a turn — force back to listening rather than leaving the
 * UI stuck in `thinking`. Armed manually on each user turn (transcript),
 * cleared by every event that legitimately leaves `thinking`.
 */
export function useThinkingFuse(onExpire: () => void) {
  return useFuseTimer(30_000, onExpire)
}

const PROCESSING_FUSE_MS = 10_000

/**
 * Processing fuse: `user_silent` -> `transcript` has no guaranteed follow-up
 * — backend drops empty ASR results without emitting any event — so
 * "辨識中…" could otherwise hang until the 45s idle path. Armed/cleared
 * automatically by watching the state itself: any transition out of
 * `processing` disarms it.
 */
export function useProcessingFuse(
  state: Readonly<Ref<ConversationState>>,
  onExpire: () => void,
) {
  const fuse = useFuseTimer(PROCESSING_FUSE_MS, onExpire)
  watch(state, next => {
    if (next === "processing") fuse.start()
    else fuse.clear()
  })
  return { clear: fuse.clear }
}
