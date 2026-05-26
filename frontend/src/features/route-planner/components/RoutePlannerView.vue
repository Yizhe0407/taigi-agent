<script setup lang="ts">
import { ArrowLeft } from "@lucide/vue"
import { useRouter } from "vue-router"

import { useRoutePlanner } from "../composables/useRoutePlanner"
import RoutePlannerMap from "./RoutePlannerMap.vue"
import RoutePlannerPanel from "./RoutePlannerPanel.vue"

const router = useRouter()
const {
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
} = useRoutePlanner()
</script>

<template>
  <div class="w-full h-full bg-kiosk-bg font-tc flex flex-col overflow-hidden">
    <!-- Kiosk top bar -->
    <div class="relative z-10 bg-kiosk-bg grid grid-cols-[auto_1fr_auto] items-center gap-5 pt-[18px] px-7 pb-4 border-b-2 border-kiosk-line shrink-0">
      <button
        class="h-[52px] pl-4 pr-[22px] bg-white border-2 border-kiosk-ink rounded-full text-[17px] font-bold text-kiosk-ink font-[inherit] cursor-pointer inline-flex items-center gap-2"
        @click="router.push('/')"
      >
        <ArrowLeft class="size-[22px]" :stroke-width="2.2" />
        <span>返回</span>
      </button>
      <div class="text-left">
        <div class="text-[13px] text-kiosk-muted font-medium mb-0.5">{{ kiosk.name }}</div>
        <div class="text-[30px] font-extrabold tracking-[-0.02em] leading-none text-kiosk-ink">路線規劃</div>
      </div>
      <div class="text-right">
        <div class="text-[13px] text-kiosk-muted font-medium mb-0.5">現在時間</div>
        <div class="text-[30px] font-bold text-kiosk-ink tabular-nums tracking-[-0.02em] leading-none font-mono">{{ nowLabel }}</div>
      </div>
    </div>

    <!-- Map + panel body -->
    <section class="flex-1 min-h-0 grid grid-cols-[minmax(0,1fr)_25rem] grid-rows-1 gap-5 pt-5 px-7 pb-6">
      <RoutePlannerMap
        class="rounded-[28px] border-2 border-kiosk-line overflow-hidden min-h-0"
        :kiosk="kiosk"
        :destination="destination"
        :route="selectedRoute"
        :moovo-stations="moovoStations"
        :is-loading-moovo-stations="isLoadingMoovoStations"
        :moovo-stations-error="moovoStationsError"
        @select-destination="selectDestination"
        @reject-destination="rejectOutOfServiceArea"
        @refresh-moovo-stations="loadMoovoStations"
      />
      <RoutePlannerPanel
        v-model:departure-mode="departureMode"
        v-model:scheduled-date-time="scheduledDateTime"
        :kiosk="kiosk"
        :destination="destination"
        :is-destination-confirmed="isDestinationConfirmed"
        :is-planning-route="isPlanningRoute"
        :route-plan="routePlan"
        :route-plan-error="routePlanError"
        :route-plan-error-kind="routePlanErrorKind"
        :selected-route="selectedRoute"
        @confirm="confirmDestination"
        @reset="resetDestination"
        @select-route="selectRoute"
      />
    </section>
  </div>
</template>
