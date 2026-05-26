<script setup lang="ts">
import { ArrowLeft, LoaderCircle } from "@lucide/vue"
import { computed } from "vue"

import { useRouteColors } from "@/lib/useRouteColors"
import { useDepartureRouteDetail } from "../composables/useDepartureRouteDetail"
import type { DepartureRouteStatus, RouteStopDetail } from "../types"

const props = defineProps<{
  route: DepartureRouteStatus
  routeColors?: Record<
    string,
    {
      hex: string
      bgClass: string
      borderClass: string
      textClass: string
    }
  >
}>()

defineEmits<{ back: [] }>()

const routeCode = computed(() => props.route.route)
const { directions, isLoading, errorMessage } =
  useDepartureRouteDetail(routeCode)

const selectedDirection = computed(
  () =>
    directions.value.find(
      (direction) => direction.goBack === props.route.goBack,
    ) ??
    directions.value[0] ??
    null,
)
const stops = computed(() => selectedDirection.value?.stops ?? [])
const currentStopIndex = computed(() =>
  stops.value.findIndex((stop) => stop.isCurrentStop),
)
const stopCountLabel = computed(() => {
  if (!stops.value.length) return "站序載入中"
  if (currentStopIndex.value >= 0) {
    return `共 ${stops.value.length} 站 · 本站第 ${currentStopIndex.value + 1} 站`
  }
  return `共 ${stops.value.length} 站`
})

function stopStatusLabel(stop: RouteStopDetail): string {
  return stop.isCurrentStop ? "本站" : stop.statusText
}
const CURRENT_STOP_BORDER_CLASS = "border-[#D86A1F] bg-[#D86A1F]"
const DEFAULT_STOP_FILL_CLASS = "bg-white"
const {
  assignments: localRouteColors,
  getRouteBgClass,
  getRouteBorderClass,
  getRouteTextClass,
} = useRouteColors(computed(() => [props.route.route]))

const routeColorAssignments = computed(
  () => props.routeColors ?? localRouteColors.value,
)

function routeBgClass(routeCode: string): string {
  return routeColorAssignments.value[routeCode]?.bgClass ?? getRouteBgClass(routeCode)
}

function routeBorderClass(routeCode: string): string {
  return routeColorAssignments.value[routeCode]?.borderClass ?? getRouteBorderClass(routeCode)
}

function routeTextClass(routeCode: string): string {
  return routeColorAssignments.value[routeCode]?.textClass ?? getRouteTextClass(routeCode)
}
</script>

<template>
  <div class="w-full h-full flex flex-col min-h-0">
    <!-- Header -->
    <div class="pt-4 px-[18px] pb-3.5 border-b-2 border-kiosk-line flex flex-col gap-3.5 shrink-0">
      <button
        class="self-start h-10 pl-2.5 pr-4 bg-kiosk-soft border-2 border-kiosk-line rounded-full text-[15px] font-bold text-kiosk-ink font-[inherit] cursor-pointer inline-flex items-center gap-2"
        @click="$emit('back')"
      >
        <ArrowLeft class="size-5" :stroke-width="2.2" />
        <span>返回</span>
      </button>
      <div class="flex items-center gap-3.5">
        <div
          class="w-16 h-16 rounded-full text-white text-xl font-extrabold tracking-[-0.02em] tabular-nums inline-flex items-center justify-center shrink-0"
          :class="routeBgClass(route.route)"
        >{{ route.route }}</div>
        <div class="flex-1 min-w-0">
          <div class="flex items-baseline gap-2">
            <span class="text-base text-kiosk-muted font-medium">往</span>
            <span class="text-[26px] font-extrabold text-kiosk-ink tracking-[-0.02em]">
              {{ selectedDirection?.label ?? route.direction }}
            </span>
          </div>
          <div class="text-[13px] text-kiosk-muted mt-1 font-medium">
            {{ stopCountLabel }}
          </div>
        </div>
      </div>
    </div>

    <div v-if="isLoading" class="flex-1 px-[18px] py-7 flex items-center justify-center gap-2.5 text-kiosk-muted text-base font-bold text-center">
      <LoaderCircle class="size-[22px] animate-spin" :stroke-width="2.4" />
      <span>載入路線站序…</span>
    </div>

    <div v-else-if="errorMessage" class="flex-1 px-[18px] py-7 flex items-center justify-center gap-2.5 text-kiosk-err text-base font-bold text-center">
      {{ errorMessage }}
    </div>

    <div v-else-if="!stops.length" class="flex-1 px-[18px] py-7 flex items-center justify-center gap-2.5 text-kiosk-muted text-base font-bold text-center">
      此路線目前沒有可顯示的站序資料
    </div>

    <!-- Stop timeline -->
    <div v-else class="flex-1 overflow-y-auto pt-2 px-[18px] pb-4">
      <div
        v-for="(s, i) in stops"
        :key="i"
        class="grid grid-cols-[32px_1fr] gap-3 items-stretch min-h-[64px]"
      >
        <!-- Track -->
        <div class="grid grid-rows-[1fr_auto_1fr] justify-items-center w-8">
          <div class="w-[3px] h-full rounded" :class="i === 0 ? 'bg-transparent' : routeBgClass(route.route)" />
          <div
            class="rounded-full border-[3px]"
            :class="[
              s.isCurrentStop ? 'w-[22px] h-[22px] shadow-[0_0_0_6px_#FCE5D2]' : 'w-[18px] h-[18px]',
              s.isCurrentStop ? CURRENT_STOP_BORDER_CLASS : DEFAULT_STOP_FILL_CLASS,
              s.isCurrentStop ? '' : routeBorderClass(route.route),
            ]"
          />
          <div class="w-[3px] h-full rounded" :class="i === stops.length - 1 ? 'bg-transparent' : routeBgClass(route.route)" />
        </div>
        <!-- Info -->
        <div class="px-3 py-2.5 bg-white border-2 border-kiosk-line rounded-[14px] flex items-center justify-between gap-3 my-2">
          <div class="flex items-center gap-3 min-w-0 flex-1">
            <div class="w-7 shrink-0">
              <span
                class="text-[13px] font-bold tabular-nums tracking-[0.05em]"
                :class="s.isCurrentStop ? 'text-[#D86A1F]' : routeTextClass(route.route)"
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
  </div>
</template>
