<script setup lang="ts">
import { ChevronRight } from "@lucide/vue"

import type { RouteColorToken } from "@/lib/route-colors"

import type { DepartureRouteStatus } from "../types"
import {
  departureDisplayState,
  departureMinutesLabel,
  statusChipClasses,
} from "../utils/departure-status"

const props = defineProps<{
  routes: DepartureRouteStatus[]
  isLoading: boolean
  routeColors: Record<string, RouteColorToken>
}>()

defineEmits<{ select: [route: DepartureRouteStatus] }>()

function bgClass(routeCode: string): string {
  return props.routeColors[routeCode]?.bgClass ?? "bg-kiosk-muted"
}
</script>

<template>
  <div class="flex justify-between items-center gap-3 px-3 pb-3.5 border-b-2 border-kiosk-line mb-3 shrink-0">
    <div class="min-w-0 text-2xl font-bold text-kiosk-ink">本站全部路線</div>
    <div class="shrink-0 text-lg text-kiosk-muted font-medium">{{ routes.length }} 條</div>
  </div>
  <div class="flex flex-col gap-2 overflow-y-auto flex-1 min-h-0">
    <button
      v-for="route in routes"
      :key="route.id"
      class="grid shrink-0 grid-cols-[76px_minmax(0,1fr)_minmax(10rem,max-content)_24px] items-center gap-3.5 px-3.5 py-3 rounded-[20px] min-h-[72px] cursor-pointer text-left font-[inherit] text-kiosk-ink w-full border-0 max-[1180px]:grid-cols-[68px_minmax(0,1fr)_22px] max-[1180px]:gap-y-1.5 max-[1180px]:min-h-[88px] max-[420px]:grid-cols-[58px_minmax(0,1fr)_18px] max-[420px]:gap-x-2.5 max-[420px]:p-3 max-[340px]:grid-cols-[52px_minmax(0,1fr)]"
      :class="route.section === 'available' ? 'bg-kiosk-accent-soft' : 'bg-kiosk-soft'"
      @click="$emit('select', route)"
    >
      <div
        class="col-start-1 row-start-1 justify-self-center w-[60px] h-[60px] rounded-full text-white text-lg font-extrabold tracking-[-0.02em] tabular-nums inline-flex items-center justify-center shrink-0 box-border overflow-hidden px-1 text-ellipsis whitespace-nowrap max-[1180px]:row-span-2 max-[420px]:w-[54px] max-[420px]:h-[54px] max-[420px]:text-base"
        :class="bgClass(route.route)"
      >{{ route.route }}</div>

      <div class="col-start-2 row-start-1 grid min-w-0 grid-cols-[auto_minmax(0,1fr)] items-center gap-2">
        <div class="justify-self-end whitespace-nowrap text-base text-kiosk-muted font-medium">往</div>
        <div class="min-w-0 overflow-hidden [display:-webkit-box] [-webkit-box-orient:vertical] [-webkit-line-clamp:2] [overflow-wrap:anywhere] text-[22px] font-bold leading-[1.15] text-kiosk-ink max-[420px]:text-lg">{{ route.direction }}</div>
      </div>

      <div
        class="col-start-3 row-start-1 justify-self-center inline-flex min-w-[7.5rem] max-w-full items-center justify-center gap-2 whitespace-nowrap py-2 px-3.5 rounded-full text-base font-bold max-[1180px]:col-start-2 max-[1180px]:row-start-2 max-[1180px]:justify-self-start max-[1180px]:self-start max-[1180px]:min-w-0 max-[420px]:py-1.5 max-[420px]:px-2.5 max-[420px]:text-sm max-[340px]:col-start-1 max-[340px]:col-span-2 max-[340px]:justify-self-center"
        :class="statusChipClasses(departureDisplayState(route)).chip"
      >
        <span
          class="w-2.5 h-2.5 rounded-full"
          :class="statusChipClasses(departureDisplayState(route)).dot"
        />
        <span class="min-w-0 overflow-hidden text-ellipsis">{{ departureMinutesLabel(route) }}</span>
      </div>

      <ChevronRight class="col-start-4 row-start-1 justify-self-center shrink-0 max-[1180px]:col-start-3 max-[1180px]:row-span-2 max-[340px]:hidden size-5 text-[#9C968A]" :stroke-width="2.2" />
    </button>

    <div v-if="!routes.length && !isLoading" class="flex items-center justify-center flex-1 text-lg font-semibold text-kiosk-muted">
      目前沒有路線資料
    </div>
  </div>
</template>
