<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from "vue"

import { fetchKiosk } from "../api/kiosk"
import { createRoutePlan, RoutePlanApiError } from "../api/route-plans"
import type { LngLat, PlaceCoordinate, RoutePlan } from "../types"
import RoutePlannerMap from "./RoutePlannerMap.vue"
import RoutePlannerPanel from "./RoutePlannerPanel.vue"

// Fallback keeps the marker visible before the API responds.
const kiosk = ref<PlaceCoordinate>({
  name: "雲林科技大學",
  coordinates: [120.5355922, 23.6940747],
})

onMounted(async () => {
  try {
    kiosk.value = await fetchKiosk()
  } catch {
    // Keep fallback — the coordinates are already set to the catalog average above.
  }
})

const destination = ref<LngLat | null>(null)
const isDestinationConfirmed = ref(false)
const isPlanningRoute = ref(false)
const routePlan = ref<RoutePlan | null>(null)
const routePlanError = ref("")
const routePlanErrorKind = ref<"no-service" | "generic">("generic")
const selectedRouteId = ref<string | null>(null)

// Departure time
const departureMode = ref<"now" | "scheduled">("now")
const scheduledDateTime = ref("") // YYYY-MM-DDTHH:mm from <input type="datetime-local">

let routeRequest: AbortController | null = null

const selectedRoute = computed(() => {
  if (!routePlan.value) return null
  return (
    routePlan.value.routes.find((route) => route.id === selectedRouteId.value) ??
    routePlan.value.routes[0] ??
    null
  )
})

const cancelRouteRequest = () => {
  routeRequest?.abort()
  routeRequest = null
  isPlanningRoute.value = false
}

const clearRoutePlan = () => {
  cancelRouteRequest()
  routePlan.value = null
  routePlanError.value = ""
  routePlanErrorKind.value = "generic"
  selectedRouteId.value = null
}

const selectDestination = (coordinates: LngLat) => {
  clearRoutePlan()
  destination.value = coordinates
  isDestinationConfirmed.value = false
}

/**
 * Parse the datetime-local input string as Taiwan time (UTC+8).
 * The input gives YYYY-MM-DDTHH:mm in the user's intended local time.
 * We always treat it as Taiwan time because the kiosk is deployed there.
 */
const getScheduledDate = (): Date | undefined => {
  if (departureMode.value === "now" || !scheduledDateTime.value) return undefined
  // Append seconds + Taiwan offset so the Date is timezone-aware
  return new Date(`${scheduledDateTime.value}:00+08:00`)
}

const confirmDestination = async () => {
  if (!destination.value) return

  const currentDestination = [...destination.value] as LngLat
  const departureDate = getScheduledDate()

  clearRoutePlan()
  isDestinationConfirmed.value = true
  isPlanningRoute.value = true
  const currentRequest = new AbortController()
  routeRequest = currentRequest

  try {
    const nextPlan = await createRoutePlan(
      currentDestination,
      departureDate,
      currentRequest.signal,
    )
    if (routeRequest !== currentRequest) return

    routePlan.value = nextPlan
    selectedRouteId.value = nextPlan.routes[0]?.id ?? null
    if (!selectedRouteId.value) {
      routePlanError.value = "路線規劃完成，但沒有可顯示的候選路線"
      routePlanErrorKind.value = "generic"
      isDestinationConfirmed.value = false
    }
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") return

    if (error instanceof RoutePlanApiError) {
      routePlanErrorKind.value = error.status === 404 ? "no-service" : "generic"
      routePlanError.value = error.message
    } else {
      routePlanErrorKind.value = "generic"
      routePlanError.value = "路線規劃失敗，請稍後再試"
    }
    isDestinationConfirmed.value = false
  } finally {
    if (routeRequest === currentRequest) {
      isPlanningRoute.value = false
      routeRequest = null
    }
  }
}

const resetDestination = () => {
  clearRoutePlan()
  destination.value = null
  isDestinationConfirmed.value = false
  departureMode.value = "now"
  scheduledDateTime.value = ""
}

onBeforeUnmount(cancelRouteRequest)
</script>

<template>
  <section
    class="grid h-full min-h-0 grid-rows-[minmax(22rem,1fr)_auto] lg:grid-cols-[minmax(0,1fr)_25rem] lg:grid-rows-1"
  >
    <RoutePlannerMap
      :kiosk="kiosk"
      :destination="destination"
      :route="selectedRoute"
      @select-destination="selectDestination"
    />
    <RoutePlannerPanel
      :kiosk="kiosk"
      :destination="destination"
      :is-destination-confirmed="isDestinationConfirmed"
      :is-planning-route="isPlanningRoute"
      :route-plan="routePlan"
      :route-plan-error="routePlanError"
      :route-plan-error-kind="routePlanErrorKind"
      :selected-route="selectedRoute"
      :departure-mode="departureMode"
      :scheduled-date-time="scheduledDateTime"
      @confirm="confirmDestination"
      @reset="resetDestination"
      @select-route="selectedRouteId = $event"
      @update:departure-mode="departureMode = $event"
      @update:scheduled-date-time="scheduledDateTime = $event"
    />
  </section>
</template>
