import { onMounted, shallowRef } from "vue"

import {
  currentSynchronousScopeReleaseAttempt,
  observeSynchronousScopeTeardown,
} from "@/lib/component-lifecycle"
import { createResourceOwner } from "@/lib/resource-owner"
import { createOwnedInterval } from "@/lib/timer-owner"

export function useNow(intervalMs = 1_000) {
  const now = shallowRef(new Date())
  const owner = createResourceOwner("current-time ticker")
  onMounted(() => {
    if (owner.disposed) return
    const setupAttempt = currentSynchronousScopeReleaseAttempt() ?? {}
    try {
      createOwnedInterval(
        owner,
        "current-time interval",
        () => {
          if (!owner.disposed) now.value = new Date()
        },
        intervalMs,
        setupAttempt,
      )
    } catch (setupError) {
      try {
        owner.dispose(setupAttempt)
      } catch (cleanupError) {
        throw new AggregateError(
          [setupError, cleanupError],
          "Failed to initialize and close current-time ticker",
        )
      }
      throw setupError
    }
  })

  observeSynchronousScopeTeardown("current-time ticker teardown", attempt => {
    owner.dispose(attempt)
    if (!owner.settled) {
      throw new Error("Current-time ticker cleanup remains unresolved")
    }
  })

  return { now }
}
