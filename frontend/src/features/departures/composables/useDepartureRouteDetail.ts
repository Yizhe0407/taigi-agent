import { useQuery } from "@tanstack/vue-query"
import { computed, type Ref } from "vue"

import { UI_FALLBACK_MESSAGES } from "@/lib/api-messages"

import {
  DeparturesApiError,
  fetchDepartureRouteDetail,
} from "../api/departures"

// Matches the dashboard's SSE push cadence (see SSE_PUSH_MS in
// useDepartureSnapshot.ts) so a user parked on the stop-sequence view sees
// per-stop ETAs refresh at the same rhythm as the route list, instead of
// freezing at whatever was current when the panel opened.
const ROUTE_DETAIL_REFETCH_MS = 20_000

export function useDepartureRouteDetail(routeCode: Ref<string>) {
  const query = useQuery({
    queryKey: ["departures", "route-detail", routeCode],
    queryFn: ({ signal }) => fetchDepartureRouteDetail(routeCode.value, signal),
    refetchInterval: ROUTE_DETAIL_REFETCH_MS,
    retry: false,
  })

  const detail = computed(() => query.data.value ?? null)
  const isLoading = computed(() => query.isLoading.value)
  const errorMessage = computed(() => {
    const err = query.error.value
    if (!err) return ""
    return err instanceof DeparturesApiError
      ? err.message
      : UI_FALLBACK_MESSAGES.routeDetailUnavailable
  })
  const directions = computed(() => detail.value?.directions ?? [])

  return { detail, directions, isLoading, errorMessage }
}
