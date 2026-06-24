import { useQuery } from "@tanstack/vue-query"
import { computed } from "vue"

import { UI_FALLBACK_MESSAGES } from "@/lib/api-messages"

import { DeparturesApiError, fetchDeparturesHere } from "../api/departures"

export function useDepartureSnapshot(refreshMs = 30_000) {
  const query = useQuery({
    queryKey: ["departures", "here"],
    queryFn: ({ signal }) => fetchDeparturesHere(signal),
    refetchInterval: refreshMs,
    retry: false,
  })

  const snapshot = computed(() => query.data.value ?? null)
  const isLoading = computed(() => query.isLoading.value)
  const errorMessage = computed(() => {
    const err = query.error.value
    if (!err) return ""
    return err instanceof DeparturesApiError
      ? err.message
      : UI_FALLBACK_MESSAGES.departuresUnavailable
  })

  /** Background refetch failed but cached data still available. */
  const hasBackgroundError = computed(
    () => !!errorMessage.value && !!snapshot.value,
  )

  const routes = computed(() => snapshot.value?.routes ?? [])
  const nextBest = computed(
    () => routes.value.find((route) => route.section === "available") ?? null,
  )

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
  }
}
