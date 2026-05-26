import { computed, onBeforeUnmount, ref, watch, type Ref } from "vue"

import {
  DeparturesApiError,
  fetchDepartureRouteDetail,
} from "../api/departures"
import type { DepartureRouteDetail } from "../types"

export function useDepartureRouteDetail(routeCode: Ref<string>) {
  const detail = ref<DepartureRouteDetail | null>(null)
  const isLoading = ref(false)
  const errorMessage = ref("")

  let abortController: AbortController | null = null

  const load = async () => {
    abortController?.abort()
    const currentRequest = new AbortController()
    abortController = currentRequest
    isLoading.value = true
    errorMessage.value = ""
    detail.value = null

    try {
      detail.value = await fetchDepartureRouteDetail(
        routeCode.value,
        currentRequest.signal,
      )
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") return
      errorMessage.value =
        error instanceof DeparturesApiError
          ? error.message
          : "路線詳情暫時無法載入"
    } finally {
      if (abortController === currentRequest) {
        abortController = null
        isLoading.value = false
      }
    }
  }

  watch(routeCode, () => void load(), { immediate: true })

  onBeforeUnmount(() => {
    abortController?.abort()
  })

  const directions = computed(() => detail.value?.directions ?? [])

  return {
    detail,
    directions,
    isLoading,
    errorMessage,
    load,
  }
}
