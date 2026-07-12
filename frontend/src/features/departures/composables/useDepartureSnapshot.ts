import { useQuery, useQueryClient } from "@tanstack/vue-query"
import { computed, onUnmounted, ref } from "vue"

import { apiBaseUrl } from "@/lib/api"
import { UI_FALLBACK_MESSAGES } from "@/lib/api-messages"

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

  const query = useQuery({
    queryKey: ["departures", "here"],
    queryFn: ({ signal }) => fetchDeparturesHere(signal),
    // SSE is the primary transport; poll only while it's disconnected.
    refetchInterval: () => (sseConnected.value ? false : REFRESH_MS),
    retry: false,
  })

  // Server push: the backend notifies right after each ETA cache refresh, so
  // the dashboard updates the moment fresh data exists instead of polling out
  // of phase. EventSource reconnects on its own; while it's down the query
  // above falls back to interval polling.
  const source = new EventSource(`${apiBaseUrl}/api/departures/stream`)
  source.onopen = () => { sseConnected.value = true }
  source.onerror = () => { sseConnected.value = false }
  source.onmessage = (event) => {
    const payload = JSON.parse(event.data) as StopDepartureSnapshot | { error: string }
    if ("error" in payload) {
      ssePushError.value = payload.error
      return
    }
    ssePushError.value = ""
    queryClient.setQueryData(["departures", "here"], payload)
  }
  onUnmounted(() => source.close())

  const now = ref(Date.now())
  const ticker = setInterval(() => { now.value = Date.now() }, 1000)
  onUnmounted(() => clearInterval(ticker))

  const secondsUntilRefresh = computed(() => {
    const intervalMs = sseConnected.value ? SSE_PUSH_MS : REFRESH_MS
    const elapsed = now.value - query.dataUpdatedAt.value
    return Math.max(0, Math.round((intervalMs - elapsed) / 1000))
  })

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
    secondsUntilRefresh,
  }
}
