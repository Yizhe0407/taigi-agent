<script setup lang="ts">
import { ArrowLeft, LoaderCircle } from "@lucide/vue"
import { computed } from "vue"

import { useRouteColors } from "@/lib/useRouteColors"

import { useDepartureRouteDetail } from "../composables/useDepartureRouteDetail"
import type { DepartureRouteStatus } from "../types"
import StopTimeline from "./StopTimeline.vue"

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

const {
  assignments: localRouteColors,
  getRouteBgClass,
  getRouteBorderClass,
  getRouteTextClass,
} = useRouteColors(computed(() => [props.route.route]))

// Prefer the shared parent assignment so colors stay consistent with the route
// list; fall back to a local assignment when the panel is mounted standalone.
const routeBgClass = computed(() => {
  const code = props.route.route
  return (
    props.routeColors?.[code]?.bgClass ??
    localRouteColors.value[code]?.bgClass ??
    getRouteBgClass(code)
  )
})
const routeBorderClass = computed(() => {
  const code = props.route.route
  return (
    props.routeColors?.[code]?.borderClass ??
    localRouteColors.value[code]?.borderClass ??
    getRouteBorderClass(code)
  )
})
const routeTextClass = computed(() => {
  const code = props.route.route
  return (
    props.routeColors?.[code]?.textClass ??
    localRouteColors.value[code]?.textClass ??
    getRouteTextClass(code)
  )
})
</script>

<template>
  <div class="w-full h-full flex flex-col min-h-0">
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
          :class="routeBgClass"
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

    <StopTimeline
      v-else
      :stops="stops"
      :bg-class="routeBgClass"
      :border-class="routeBorderClass"
      :text-class="routeTextClass"
    />
  </div>
</template>
