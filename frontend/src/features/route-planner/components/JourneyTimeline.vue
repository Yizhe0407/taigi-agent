<script setup lang="ts">
import { BusFront, Footprints } from "@lucide/vue"

import { formatTaipeiHourMinute } from "@/lib/time"

import type { RouteOption } from "../types"
import { distanceLabel, durationLabel } from "../utils/route-display"

defineProps<{
  route: RouteOption
  legClassForRoute: (leg: RouteOption["legs"][number]) => string
}>()

const timeLabel = (iso: string): string => formatTaipeiHourMinute(new Date(iso))
</script>

<template>
  <div class="flex flex-col">
    <div class="flex justify-between items-baseline px-1 pb-2.5">
      <span class="text-lg font-extrabold text-kiosk-ink tracking-[-0.01em]">行程</span>
      <span class="text-[13px] text-kiosk-muted font-semibold">共 {{ durationLabel(route.duration) }}</span>
    </div>
    <div class="bg-kiosk-soft rounded-[20px] py-4 px-[18px]">
      <div
        v-for="(leg, i) in route.legs"
        :key="`${leg.mode}-${leg.start}-${i}`"
        class="grid grid-cols-[44px_1fr] gap-3.5 items-stretch"
      >
        <div class="flex flex-col items-center">
          <div
            class="w-11 h-11 rounded-full inline-flex items-center justify-center shrink-0"
            :class="legClassForRoute(leg)"
          >
            <BusFront
              v-if="leg.mode === 'BUS'"
              class="size-[18px] text-white"
              :stroke-width="2"
            />
            <Footprints
              v-else
              class="size-[18px] text-white"
              :stroke-width="2"
            />
          </div>
          <div
            v-if="i < route.legs.length - 1"
            class="flex-1 w-0.5 bg-kiosk-line my-1 rounded min-h-3"
          />
        </div>
        <div :class="i === route.legs.length - 1 ? 'pb-1' : 'pb-4'">
          <div class="flex justify-between items-center gap-2 mb-1 flex-wrap">
            <div class="inline-flex items-center gap-1.5">
              <span
                v-if="leg.mode === 'BUS' && leg.route?.shortName"
                class="py-[3px] px-2.5 text-white text-xs font-extrabold rounded-full tracking-[-0.02em] tabular-nums"
                :class="legClassForRoute(leg)"
              >{{ leg.route.shortName }}</span>
              <span class="text-xs text-kiosk-muted font-bold tracking-[0.05em]">{{ leg.mode === 'BUS' ? '公車' : '步行' }}</span>
            </div>
            <div class="text-[13px] font-bold text-kiosk-ink tabular-nums font-mono whitespace-nowrap">
              {{ timeLabel(leg.start) }}<span class="text-kiosk-faded"> — </span>{{ timeLabel(leg.end) }}
            </div>
          </div>
          <div class="flex items-baseline gap-1.5 flex-wrap mt-0.5">
            <span class="text-[17px] font-extrabold text-kiosk-ink tracking-[-0.01em]">{{ leg.fromName }}</span>
            <span class="text-[17px] text-kiosk-faded font-medium">→</span>
            <span class="text-[17px] font-extrabold text-kiosk-ink tracking-[-0.01em]">{{ leg.toName }}</span>
          </div>
          <div class="text-xs text-kiosk-muted font-semibold mt-1">
            {{ durationLabel(leg.duration) }} · {{ distanceLabel(leg.distance) }}
          </div>
        </div>
      </div>
    </div>
  </div>
</template>
