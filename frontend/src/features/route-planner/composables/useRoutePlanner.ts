import { useMutation, useQuery } from "@tanstack/vue-query"
import { computed, ref } from "vue"

import { UI_FALLBACK_MESSAGES } from "@/lib/api-messages"
import { formatTaipeiHourMinute, parseTaipeiDateTimeInput } from "@/lib/time"
import { useNow } from "@/lib/useNow"

import { fetchKiosk } from "../api/kiosk"
import { fetchMoovoStations } from "../api/moovo"
import { createRoutePlan, RoutePlanApiError } from "../api/route-plans"
import { isInYunlinCounty } from "../geo/yunlin-service-area"
import type { KioskPlace, LngLat, RoutePlan } from "../types"
import type { DepartureMode } from "./useScheduledTimeWheel"

const FALLBACK_KIOSK: KioskPlace = {
  name: "雲林科技大學",
  coordinates: [120.5355922, 23.6940747],
  direction: "回程",
}

export function useRoutePlanner() {
  // —— remote queries ——
  const kioskQuery = useQuery({
    queryKey: ["route-planner", "kiosk"],
    queryFn: () => fetchKiosk(),
    initialData: { ...FALLBACK_KIOSK },
    retry: 1,
  })
  const moovoQuery = useQuery({
    queryKey: ["route-planner", "moovo-stations"],
    queryFn: ({ signal }) => fetchMoovoStations(signal),
    retry: false,
  })
  const kiosk = computed<KioskPlace>(() => kioskQuery.data.value ?? FALLBACK_KIOSK)
  const moovoStations = computed(() => moovoQuery.data.value ?? [])
  const isLoadingMoovoStations = computed(() => moovoQuery.isLoading.value)
  const moovoStationsError = computed(() =>
    moovoQuery.error.value ? UI_FALLBACK_MESSAGES.moovoUnavailable : "",
  )

  // —— local form / selection state ——
  const destination = ref<LngLat | null>(null)
  const isDestinationConfirmed = ref(false)
  const routePlan = ref<RoutePlan | null>(null)
  const routePlanError = ref("")
  const routePlanErrorKind = ref<"no-service" | "generic">("generic")
  const selectedRouteId = ref<string | null>(null)
  const departureMode = ref<DepartureMode>("now")
  const scheduledDateTime = ref("")

  // Manual AbortController for the route-plan mutation: useMutation has no built-in
  // signal, but we need to cancel in-flight requests when the user re-confirms.
  let routePlanAbort: AbortController | null = null

  const planMutation = useMutation({
    mutationFn: (vars: {
      destination: LngLat
      departureDate?: Date
      signal: AbortSignal
    }) => createRoutePlan(vars.destination, vars.departureDate, vars.signal),
  })
  const isPlanningRoute = computed(() => planMutation.isPending.value)

  const { now } = useNow(10_000)
  const nowLabel = computed(() => formatTaipeiHourMinute(now.value))

  const selectedRoute = computed(() => {
    if (!routePlan.value) return null
    return (
      routePlan.value.routes.find((r) => r.id === selectedRouteId.value) ??
      routePlan.value.routes[0] ??
      null
    )
  })

  function cancelRouteRequest() {
    routePlanAbort?.abort()
    routePlanAbort = null
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

    const controller = new AbortController()
    routePlanAbort = controller

    try {
      const plan = await planMutation.mutateAsync({
        destination: currentDestination,
        departureDate,
        signal: controller.signal,
      })
      if (routePlanAbort !== controller) return

      routePlan.value = plan
      selectedRouteId.value = plan.routes[0]?.id ?? null
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
      if (routePlanAbort === controller) routePlanAbort = null
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
    loadMoovoStations: () => void moovoQuery.refetch(),
    selectDestination,
    rejectOutOfServiceArea,
    confirmDestination,
    resetDestination,
    selectRoute,
  }
}
