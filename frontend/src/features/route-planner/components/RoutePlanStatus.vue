<script setup lang="ts">
import {
  ArrowRight,
  CircleAlert,
  LoaderCircle,
  MapPin,
  TriangleAlert,
} from "@lucide/vue"

import type { LngLat, RoutePlan } from "../types"
import type { DepartureMode } from "../composables/useScheduledTimeWheel"

defineProps<{
  destination: LngLat | null
  departureMode: DepartureMode
  isDestinationConfirmed: boolean
  isPlanningRoute: boolean
  routePlan: RoutePlan | null
  routePlanError: string
  routePlanErrorKind: "no-service" | "generic"
  scheduledDateTime: string
}>()

defineEmits<{ confirm: [] }>()
</script>

<template>
  <button
    v-if="destination && !isDestinationConfirmed && !isPlanningRoute"
    class="h-14 bg-kiosk-accent text-white border-0 rounded-[18px] text-lg font-extrabold font-[inherit] cursor-pointer flex items-center justify-center gap-2.5 shrink-0 tracking-[-0.01em] disabled:opacity-45 disabled:cursor-default"
    :disabled="departureMode === 'scheduled' && !scheduledDateTime"
    @click="$emit('confirm')"
  >
    <ArrowRight class="size-5" :stroke-width="2.5" />
    規劃路線
  </button>

  <div
    v-else-if="isPlanningRoute"
    class="rounded-[18px] py-3.5 px-4 flex items-center gap-3 text-sm font-semibold shrink-0 bg-kiosk-soft text-kiosk-muted"
  >
    <LoaderCircle class="size-[22px] animate-spin" :stroke-width="2.5" />
    <span>正在規劃路線…</span>
  </div>

  <div
    v-else-if="routePlanError && routePlanErrorKind === 'no-service'"
    class="rounded-[18px] py-3.5 px-4 flex items-start gap-3 text-sm font-semibold shrink-0 bg-kiosk-warn-soft text-kiosk-warn"
  >
    <CircleAlert class="size-5 shrink-0" :stroke-width="2.2" />
    <div>
      <div class="text-[15px] font-extrabold text-kiosk-ink mb-[3px]">此時段無班次</div>
      <div class="text-[13px] text-kiosk-muted font-medium">請調整出發時間或選擇其他目的地</div>
    </div>
  </div>

  <div
    v-else-if="routePlanError"
    class="rounded-[18px] py-3.5 px-4 flex items-start gap-3 text-sm font-semibold shrink-0 bg-kiosk-err-soft text-kiosk-err"
  >
    <TriangleAlert class="size-5 shrink-0" :stroke-width="2.2" />
    <span>{{ routePlanError }}</span>
  </div>

  <div
    v-else-if="!destination && !routePlan"
    class="rounded-[18px] py-7 px-4 flex flex-col items-center justify-center gap-2.5 shrink-0 bg-kiosk-soft text-kiosk-faded text-[15px] font-semibold text-center"
  >
    <MapPin class="size-7 shrink-0" :stroke-width="2" />
    <span>請在地圖上點選目的地</span>
  </div>
</template>
