<script setup lang="ts">
import { computed } from "vue"

import { useRouteColors } from "@/lib/useRouteColors"

import { useScheduledTimeWheel, type DepartureMode } from "../composables/useScheduledTimeWheel"
import type { LngLat, PlaceCoordinate, RouteOption, RoutePlan } from "../types"
import { routePrimaryId } from "../utils/route-display"
import DepartureTimeSelector from "./DepartureTimeSelector.vue"
import JourneyTimeline from "./JourneyTimeline.vue"
import OriginDestinationCard from "./OriginDestinationCard.vue"
import RouteOptionList from "./RouteOptionList.vue"
import RoutePlanStatus from "./RoutePlanStatus.vue"
import ScheduledTimeWheelSheet from "./ScheduledTimeWheelSheet.vue"

const props = defineProps<{
  kiosk: PlaceCoordinate
  destination: LngLat | null
  isDestinationConfirmed: boolean
  isPlanningRoute: boolean
  routePlan: RoutePlan | null
  routePlanError: string
  routePlanErrorKind: "no-service" | "generic"
  selectedRoute: RouteOption | null
}>()

defineEmits<{
  confirm: []
  reset: []
  "select-route": [routeId: string]
}>()

const departureMode = defineModel<DepartureMode>("departureMode", {
  required: true,
})
const scheduledDateTime = defineModel<string>("scheduledDateTime", {
  required: true,
})
const {
  sheetOpen,
  pendingHour,
  pendingMinute,
  openSheet,
  handleHourScroll,
  handleMinuteScroll,
  confirmSheet,
} = useScheduledTimeWheel(scheduledDateTime, departureMode)

const scheduledTimeDisplay = computed((): string | null => {
  if (!scheduledDateTime.value) return null
  const t = scheduledDateTime.value.split("T")[1]
  return t ? t.substring(0, 5) : null
})

const destinationLabel = computed((): string | null => {
  if (props.routePlan?.destination?.name) return props.routePlan.destination.name
  if (props.destination)
    return `${props.destination[1].toFixed(5)}, ${props.destination[0].toFixed(5)}`
  return null
})
const { assignments: routeOptionColorAssignments, getRouteBgClass } = useRouteColors(
  computed(() => props.routePlan?.routes.map((route) => routePrimaryId(route)) ?? []),
)

const BUS_BG_CLASS = "bg-[#1F5BBF]"
const WALK_BG_CLASS = "bg-[#9C968A]"

function legBgClass(leg: RouteOption["legs"][number]): string {
  if (leg.mode !== "BUS") return WALK_BG_CLASS
  const routeCode = leg.route?.shortName ?? leg.route?.longName
  if (!routeCode) return BUS_BG_CLASS
  return routeOptionColorAssignments.value[routeCode]?.bgClass ?? getRouteBgClass(routeCode)
}

function routeOptionBadgeClass(route: RouteOption): string {
  const routeCode = routePrimaryId(route)
  return routeOptionColorAssignments.value[routeCode]?.bgClass ?? getRouteBgClass(routeCode)
}
</script>

<template>
  <aside class="bg-white border-2 border-kiosk-line rounded-[28px] pt-5 px-5 pb-4 flex flex-col gap-4 overflow-y-auto min-h-0">
    <OriginDestinationCard
      :origin-name="kiosk.name"
      :destination="destination"
      :destination-label="destinationLabel"
      @reset="$emit('reset')"
    />

    <DepartureTimeSelector
      v-model:departure-mode="departureMode"
      :scheduled-time-display="scheduledTimeDisplay"
      @open-sheet="openSheet"
    />

    <RoutePlanStatus
      :destination="destination"
      :departure-mode="departureMode"
      :is-destination-confirmed="isDestinationConfirmed"
      :is-planning-route="isPlanningRoute"
      :route-plan="routePlan"
      :route-plan-error="routePlanError"
      :route-plan-error-kind="routePlanErrorKind"
      :scheduled-date-time="scheduledDateTime"
      @confirm="$emit('confirm')"
    />

    <template v-if="routePlan && selectedRoute">
      <RouteOptionList
        :routes="routePlan.routes"
        :selected-route-id="selectedRoute.id"
        :badge-class-for-route="routeOptionBadgeClass"
        @select-route="$emit('select-route', $event)"
      />

      <JourneyTimeline
        :route="selectedRoute"
        :leg-class-for-route="legBgClass"
      />
    </template>

    <ScheduledTimeWheelSheet
      v-model:open="sheetOpen"
      :pending-hour="pendingHour"
      :pending-minute="pendingMinute"
      @hour-scroll="handleHourScroll"
      @minute-scroll="handleMinuteScroll"
      @confirm="confirmSheet"
    />
  </aside>
</template>
