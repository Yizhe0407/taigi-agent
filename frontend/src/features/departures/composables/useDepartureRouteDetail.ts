import { useQuery } from "@tanstack/vue-query"
import { computed, type Ref } from "vue"

import {
  DeparturesApiError,
  fetchDepartureRouteDetail,
} from "../api/departures"

export function useDepartureRouteDetail(routeCode: Ref<string>) {
  const query = useQuery({
    queryKey: ["departures", "route-detail", routeCode],
    queryFn: ({ signal }) => fetchDepartureRouteDetail(routeCode.value, signal),
    retry: false,
  })

  const detail = computed(() => query.data.value ?? null)
  const isLoading = computed(() => query.isLoading.value)
  const errorMessage = computed(() => {
    const err = query.error.value
    if (!err) return ""
    return err instanceof DeparturesApiError
      ? err.message
      : "路線詳情暫時無法載入"
  })
  const directions = computed(() => detail.value?.directions ?? [])

  return { detail, directions, isLoading, errorMessage }
}
