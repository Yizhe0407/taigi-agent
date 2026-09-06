import { useQuery, useQueryClient } from "@tanstack/vue-query"
import { computed, ref } from "vue"

import { apiBaseUrl } from "@/lib/api"
import { UI_FALLBACK_MESSAGES } from "@/lib/api-messages"
import {
  currentSynchronousScopeReleaseAttempt,
  observeSynchronousScopeTeardown,
} from "@/lib/component-lifecycle"
import { reportClientEvent } from "@/lib/report-client-event"
import {
  createResourceOwner,
  type ResourceReleaseAttempt,
} from "@/lib/resource-owner"

import { DeparturesApiError, fetchDeparturesHere } from "../api/departures"
import type { StopDepartureSnapshot } from "../types"

/** Polling cadence, used only while the SSE stream is down. */
const REFRESH_MS = 15_000
/** Backend ETA warmup tick cadence — SSE pushes land on this rhythm. */
const SSE_PUSH_MS = 25_000

export function useDepartureSnapshot() {
  const queryClient = useQueryClient()
  const sseConnected = ref(false)
  const ssePushError = ref("")
  const sseOwner = createResourceOwner("departure snapshot EventSource")

  observeSynchronousScopeTeardown("departure snapshot teardown", (attempt) => {
    const failures: unknown[] = []

    try {
      sseConnected.value = false
    } catch (failure) {
      failures.push(failure)
    }

    let ownerDisposeFailed = false
    try {
      sseOwner.dispose(attempt)
    } catch (failure) {
      ownerDisposeFailed = true
      failures.push(failure)
    }

    if (!ownerDisposeFailed && !sseOwner.settled) {
      failures.push(
        new Error("Departure snapshot EventSource cleanup remains unresolved"),
      )
    }

    if (failures.length === 1) throw failures[0]
    if (failures.length > 1) {
      throw new AggregateError(
        failures,
        "Failed to tear down departure snapshot EventSource",
      )
    }
  })

  const query = useQuery({
    queryKey: ["departures", "here"],
    queryFn: ({ signal }) => fetchDeparturesHere(signal),
    // SSE is the primary transport; poll only while it's disconnected.
    refetchInterval: () => (sseConnected.value ? false : REFRESH_MS),
    retry: false,
  })

  // Server push: the backend notifies right after each ETA cache refresh, so
  // the dashboard updates the moment fresh data exists instead of polling out
  // of phase. EventSource reconnects on its own; while it is down the query
  // above falls back to interval polling. Every callback and the connection
  // itself are individually claimed before their imperative setup step.
  const setupAttempt: ResourceReleaseAttempt =
    currentSynchronousScopeReleaseAttempt() ?? {}
  try {
    const sourceAcquisition = sseOwner.acquire(
      () => new EventSource(`${apiBaseUrl}/api/departures/stream`),
      (ownedSource) => {
        ownedSource?.close()
      },
      "connection",
      setupAttempt,
    )
    const ownedSource = sourceAcquisition.value

    const handleOpen = () => {
      if (sseOwner.disposed) return
      sseConnected.value = true
    }
    const handleError = () => {
      if (sseOwner.disposed) return
      sseConnected.value = false
    }
    const handleMessage = (event: MessageEvent) => {
      if (sseOwner.disposed) return
      let payload: StopDepartureSnapshot | { error: string }
      try {
        payload = JSON.parse(event.data) as StopDepartureSnapshot | { error: string }
      } catch (error) {
        ssePushError.value = UI_FALLBACK_MESSAGES.departuresUnavailable
        reportClientEvent(
          "departures_sse_invalid_json",
          error instanceof Error ? error.message : String(error),
        )
        return
      }
      if (!payload || typeof payload !== "object") {
        ssePushError.value = UI_FALLBACK_MESSAGES.departuresUnavailable
        reportClientEvent("departures_sse_invalid_payload", "SSE payload is not an object")
        return
      }
      if ("error" in payload) {
        ssePushError.value = payload.error
        return
      }
      ssePushError.value = ""
      queryClient.setQueryData(["departures", "here"], payload)
    }

    sseOwner.acquire(
      () => { ownedSource.onopen = handleOpen },
      () => { ownedSource.onopen = null },
      "open callback",
      setupAttempt,
    )
    sseOwner.acquire(
      () => { ownedSource.onerror = handleError },
      () => { ownedSource.onerror = null },
      "error callback",
      setupAttempt,
    )
    sseOwner.acquire(
      () => { ownedSource.onmessage = handleMessage },
      () => { ownedSource.onmessage = null },
      "message callback",
      setupAttempt,
    )
  } catch (setupError) {
    try {
      sseOwner.dispose(setupAttempt)
    } catch (cleanupError) {
      throw new AggregateError(
        [setupError, cleanupError],
        "Failed to initialize and roll back departure snapshot EventSource",
      )
    }
    throw setupError
  }

  // Countdown-to-refresh is only ever displayed by a small badge; the ticker
  // that drives it lives there (RouteRefreshCountdown.vue), not here, so a
  // once-a-second tick doesn't force this composable's much larger consumer
  // tree (the whole dashboard) to re-render every second. This composable
  // only exposes the two slow-changing inputs the badge needs.
  const refreshIntervalMs = computed(() => (sseConnected.value ? SSE_PUSH_MS : REFRESH_MS))

  const snapshot = computed(() => query.data.value ?? null)
  const isLoading = computed(() => query.isLoading.value)
  const errorMessage = computed(() => {
    const err = query.error.value
    if (err) {
      return err instanceof DeparturesApiError
        ? err.message
        : UI_FALLBACK_MESSAGES.departuresUnavailable
    }
    return ssePushError.value
  })

  /** Background refetch failed but cached data still available. */
  const hasBackgroundError = computed(
    () => !!errorMessage.value && !!snapshot.value,
  )

  const routes = computed(() =>
    (snapshot.value?.routes ?? []).filter(
      (r) => r.section === "available" || r.section === "not_departed",
    ),
  )
  const nextBest = computed(() => routes.value[0] ?? null)

  /**
   * True when the last bus of every route has departed and no route is still
   * available or waiting to depart — i.e. the kiosk has no service until
   * tomorrow morning.
   */
  const isAllClosed = computed(() => {
    const s = snapshot.value?.summary
    if (!s) return false
    return s.availableCount === 0 && s.notDepartedCount === 0 && s.lastDepartedCount > 0
  })

  return {
    snapshot,
    isLoading,
    errorMessage,
    hasBackgroundError,
    routes,
    nextBest,
    isAllClosed,
    dataUpdatedAt: query.dataUpdatedAt,
    refreshIntervalMs,
  }
}
