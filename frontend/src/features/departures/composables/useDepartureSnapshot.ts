import { computed, onBeforeUnmount, onMounted, ref } from "vue"

import { DeparturesApiError, fetchDeparturesHere } from "../api/departures"
import type { StopDepartureSnapshot } from "../types"

export function useDepartureSnapshot(refreshMs = 30_000) {
  const snapshot = ref<StopDepartureSnapshot | null>(null)
  const isLoading = ref(false)
  const errorMessage = ref("")

  let refreshTimer: ReturnType<typeof setInterval> | null = null
  let abortController: AbortController | null = null

  const routes = computed(() => snapshot.value?.routes ?? [])
  const nextBest = computed(
    () => routes.value.find((route) => route.section === "available") ?? null,
  )

  async function load(silent = false) {
    abortController?.abort()
    const currentRequest = new AbortController()
    abortController = currentRequest
    if (!silent) isLoading.value = true
    errorMessage.value = ""

    try {
      snapshot.value = await fetchDeparturesHere(currentRequest.signal)
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") return
      errorMessage.value =
        error instanceof DeparturesApiError
          ? error.message
          : "公車資訊暫時無法載入"
    } finally {
      if (abortController === currentRequest) {
        abortController = null
        isLoading.value = false
      }
    }
  }

  onMounted(() => {
    void load()
    refreshTimer = setInterval(() => void load(true), refreshMs)
  })

  onBeforeUnmount(() => {
    abortController?.abort()
    if (refreshTimer) clearInterval(refreshTimer)
  })

  return {
    snapshot,
    isLoading,
    errorMessage,
    routes,
    nextBest,
    load,
  }
}
