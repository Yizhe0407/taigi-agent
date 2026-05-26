import { useQuery } from "@tanstack/vue-query"
import { computed } from "vue"

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
      : "公車資訊暫時無法載入"
  })
  const routes = computed(() => snapshot.value?.routes ?? [])
  const nextBest = computed(
    () => routes.value.find((route) => route.section === "available") ?? null,
  )

  return { snapshot, isLoading, errorMessage, routes, nextBest }
}
