<script setup lang="ts">
import type { RouteOption } from "../types"
import {
  distanceLabel,
  routePrimaryId,
  routeSecondaryLabel,
} from "../utils/route-display"

defineProps<{
  routes: RouteOption[]
  selectedRouteId: string
  badgeClassForRoute: (route: RouteOption) => string
}>()

defineEmits<{ "select-route": [routeId: string] }>()
</script>

<template>
  <div class="flex flex-col">
    <div class="flex justify-between items-baseline px-1 pb-2.5">
      <span class="text-lg font-extrabold text-kiosk-ink tracking-[-0.01em]">推薦路線</span>
      <span class="text-[13px] text-kiosk-muted font-semibold">{{ routes.length }} 個方案</span>
    </div>
    <div class="flex flex-col gap-2">
      <button
        v-for="route in routes"
        :key="route.id"
        class="grid grid-cols-[52px_1fr_auto] items-center gap-3.5 py-3 px-3.5 rounded-[18px] cursor-pointer font-[inherit] text-kiosk-ink text-left min-h-[72px] border-2"
        :class="route.id === selectedRouteId
          ? 'bg-kiosk-accent-soft border-kiosk-accent'
          : 'bg-kiosk-soft border-kiosk-soft'"
        @click="$emit('select-route', route.id)"
      >
        <div
          class="w-12 h-12 rounded-full text-white text-sm font-extrabold tabular-nums tracking-[-0.02em] inline-flex items-center justify-center shrink-0"
          :class="badgeClassForRoute(route)"
        >
          {{ routePrimaryId(route) }}
        </div>
        <div class="flex-1 min-w-0">
          <div class="flex items-baseline gap-1">
            <span class="text-[26px] font-extrabold text-kiosk-ink tabular-nums tracking-[-0.02em]">{{ Math.max(1, Math.round(route.duration / 60)) }}</span>
            <span class="text-sm font-semibold text-kiosk-ink ml-0.5">分鐘</span>
            <span class="text-sm text-kiosk-line2 mx-1">·</span>
            <span class="text-[13px] text-kiosk-muted font-semibold tabular-nums">{{ distanceLabel(route.distance) }}</span>
          </div>
          <div class="text-[13px] text-kiosk-muted font-medium mt-0.5">
            {{ routeSecondaryLabel(route) }}
          </div>
        </div>
        <div
          v-if="route.id === selectedRouteId"
          class="w-7 h-7 rounded-full bg-kiosk-accent text-white inline-flex items-center justify-center text-[15px] font-extrabold shrink-0"
        >✓</div>
      </button>
    </div>
  </div>
</template>
