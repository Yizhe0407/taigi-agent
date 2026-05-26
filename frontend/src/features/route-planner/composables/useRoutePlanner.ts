import { computed, onBeforeUnmount, onMounted, ref } from "vue"

import { formatTaipeiHourMinute, parseTaipeiDateTimeInput } from "@/lib/time"
import { useNow } from "@/lib/useNow"

import { fetchKiosk } from "../api/kiosk"
import { fetchMoovoStations } from "../api/moovo"
import { createRoutePlan, RoutePlanApiError } from "../api/route-plans"
import { isInYunlinCounty } from "../geo/yunlin-service-area"
import type { LngLat, MoovoStation, PlaceCoordinate, RoutePlan } from "../types"
import type { DepartureMode } from "./useScheduledTimeWheel"

const FALLBACK_KIOSK: PlaceCoordinate = {
  name: "雲林科技大學",
  coordinates: [120.5355922, 23.6940747],
}

export function useRoutePlanner() {
  const kiosk = ref<PlaceCoordinate>({ ...FALLBACK_KIOSK })
  const destination = ref<LngLat | null>(null)
  const isDestinationConfirmed = ref(false)
  const isPlanningRoute = ref(false)
  const routePlan = ref<RoutePlan | null>(null)
  const routePlanError = ref("")
  const routePlanErrorKind = ref<"no-service" | "generic">("generic")
  const selectedRouteId = ref<string | null>(null)
  const moovoStations = ref<MoovoStation[]>([])
  const isLoadingMoovoStations = ref(false)
  const moovoStationsError = ref("")
  const departureMode = ref<DepartureMode>("now")
  const scheduledDateTime = ref("")

  let routeRequest: AbortController | null = null
  let moovoRequest: AbortController | null = null

  const { now } = useNow(10_000)
  const nowLabel = computed(() => formatTaipeiHourMinute(now.value))

  const selectedRoute = computed(() => {
    if (!routePlan.value) return null
    return (
      routePlan.value.routes.find((route) => route.id === selectedRouteId.value) ??
      routePlan.value.routes[0] ??
      null
    )
  })

  function cancelRouteRequest() {
    routeRequest?.abort()
    routeRequest = null
    isPlanningRoute.value = false
  }

  async function loadKiosk() {
    try {
      kiosk.value = await fetchKiosk()
    } catch {
      kiosk.value = { ...FALLBACK_KIOSK }
    }
  }

  async function loadMoovoStations() {
    moovoRequest?.abort()
    const currentRequest = new AbortController()
    moovoRequest = currentRequest
    isLoadingMoovoStations.value = true
    moovoStationsError.value = ""

    try {
      const stations = await fetchMoovoStations(currentRequest.signal)
      if (moovoRequest !== currentRequest) return
      moovoStations.value = stations
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") return
      moovoStationsError.value = "MOOVO 站點暫時無法載入"
    } finally {
      if (moovoRequest === currentRequest) {
        isLoadingMoovoStations.value = false
        moovoRequest = null
      }
    }
  }

  function clearRoutePlan() {
    cancelRouteRequest()
    routePlan.value = null
    routePlanError.value = ""
    routePlanErrorKind.value = "generic"
    selectedRouteId.value = null
  }

  function rejectOutOfServiceArea() {
    clearRoutePlan()
    routePlanError.value = "目的地超出範圍，請選擇雲林縣內的地點"
    routePlanErrorKind.value = "generic"
  }

  function selectDestination(coordinates: LngLat) {
    if (!isInYunlinCounty(coordinates)) {
      rejectOutOfServiceArea()
      return
    }

    clearRoutePlan()
    destination.value = coordinates
    isDestinationConfirmed.value = false
  }

  function scheduledDepartureDate(): Date | undefined {
    if (departureMode.value === "now") return undefined
    return parseTaipeiDateTimeInput(scheduledDateTime.value)
  }

  async function confirmDestination() {
    if (!destination.value) return

    const currentDestination = [...destination.value] as LngLat
    const departureDate = scheduledDepartureDate()

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

  function resetDestination() {
    clearRoutePlan()
    destination.value = null
    isDestinationConfirmed.value = false
    departureMode.value = "now"
    scheduledDateTime.value = ""
  }

  function selectRoute(routeId: string) {
    selectedRouteId.value = routeId
  }

  onMounted(() => {
    void loadKiosk()
    void loadMoovoStations()
  })

  onBeforeUnmount(() => {
    cancelRouteRequest()
    moovoRequest?.abort()
  })

  return {
    kiosk,
    destination,
    isDestinationConfirmed,
    isPlanningRoute,
    routePlan,
    routePlanError,
    routePlanErrorKind,
    selectedRoute,
    moovoStations,
    isLoadingMoovoStations,
    moovoStationsError,
    departureMode,
    scheduledDateTime,
    nowLabel,
    loadMoovoStations,
    selectDestination,
    rejectOutOfServiceArea,
    confirmDestination,
    resetDestination,
    selectRoute,
  }
}
