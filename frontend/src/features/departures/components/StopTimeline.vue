<script setup lang="ts">
import type { RouteStopDetail } from "../types"

const props = defineProps<{
  stops: RouteStopDetail[]
  bgClass: string
  borderClass: string
  textClass: string
}>()

const CURRENT_STOP_BORDER_CLASS = "border-[#D86A1F] bg-[#D86A1F]"
const DEFAULT_STOP_FILL_CLASS = "bg-white"

function stopStatusLabel(stop: RouteStopDetail): string {
  return stop.isCurrentStop ? "本站" : stop.statusText
}
</script>

<template>
  <div class="flex-1 overflow-y-auto pt-2 px-[18px] pb-4">
    <div
      v-for="(s, i) in stops"
      :key="i"
      class="grid grid-cols-[32px_1fr] gap-3 items-stretch min-h-[64px]"
    >
      <!-- Track -->
      <div class="grid grid-rows-[1fr_auto_1fr] justify-items-center w-8">
        <div class="w-[3px] h-full rounded" :class="i === 0 ? 'bg-transparent' : bgClass" />
        <div
          class="rounded-full border-[3px]"
          :class="[
            s.isCurrentStop ? 'w-[22px] h-[22px] shadow-[0_0_0_6px_#FCE5D2]' : 'w-[18px] h-[18px]',
            s.isCurrentStop ? CURRENT_STOP_BORDER_CLASS : DEFAULT_STOP_FILL_CLASS,
            s.isCurrentStop ? '' : borderClass,
          ]"
        />
        <div class="w-[3px] h-full rounded" :class="i === props.stops.length - 1 ? 'bg-transparent' : bgClass" />
      </div>
      <!-- Info -->
      <div class="px-3 py-2.5 bg-white border-2 border-kiosk-line rounded-[14px] flex items-center justify-between gap-3 my-2">
        <div class="flex items-center gap-3 min-w-0 flex-1">
          <div class="w-7 shrink-0">
            <span
              class="text-[13px] font-bold tabular-nums tracking-[0.05em]"
              :class="s.isCurrentStop ? 'text-[#D86A1F]' : textClass"
            >
              {{ String(s.seq).padStart(2, '0') }}
            </span>
          </div>
          <div class="min-w-0">
            <div
              class="text-lg font-extrabold tracking-[-0.01em] flex items-baseline gap-2"
              :class="s.isCurrentStop ? 'text-[#D86A1F]' : 'text-kiosk-ink'"
            >
              {{ s.name }}
              <span
                v-if="i === stops.length - 1"
                class="text-[11px] font-bold text-white bg-kiosk-ink px-2 py-[2px] rounded-full tracking-[0.05em]"
              >終點</span>
            </div>
          </div>
        </div>
        <div class="text-sm font-bold text-kiosk-muted shrink-0 font-mono tabular-nums">
          <span
            v-if="s.isCurrentStop"
            class="inline-flex items-center gap-1.5 py-[5px] px-[11px] bg-kiosk-accent-soft text-kiosk-accent rounded-full text-xs font-bold"
          >
            <span class="w-[7px] h-[7px] rounded-full bg-kiosk-accent" />
            {{ stopStatusLabel(s) }}
          </span>
          <span v-else>{{ stopStatusLabel(s) }}</span>
        </div>
      </div>
    </div>
  </div>
</template>
