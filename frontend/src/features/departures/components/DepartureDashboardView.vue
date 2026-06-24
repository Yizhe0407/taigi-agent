<script setup lang="ts">
import { LoaderCircle, WifiOff } from "@lucide/vue"
import { computed, ref } from "vue"

import { formatTaipeiHourMinute } from "@/lib/time"
import { useNow } from "@/lib/useNow"
import { useRouteColors } from "@/lib/useRouteColors"

import { useDepartureSnapshot } from "../composables/useDepartureSnapshot"
import type { DepartureRouteStatus } from "../types"
import DepartureHeroCard from "./DepartureHeroCard.vue"
import NoServiceHeroCard from "./NoServiceHeroCard.vue"
import RouteDetailPanel from "./RouteDetailPanel.vue"
import RouteList from "./RouteList.vue"
const selectedRoute = ref<DepartureRouteStatus | null>(null)
const {
  snapshot,
  isLoading,
  errorMessage,
  hasBackgroundError,
  routes,
  nextBest,
  isAllClosed,
} = useDepartureSnapshot()
const { now } = useNow()
const nowText = computed(() => formatTaipeiHourMinute(now.value))
const { assignments: routeColorAssignments } = useRouteColors(
  computed(() => routes.value.map((route) => route.route)),
)

const lastUpdatedText = computed(() => {
  const t = snapshot.value?.updatedAt
  if (!t) return null
  return formatTaipeiHourMinute(new Date(t))
})
</script>

<template>
  <div class="w-full h-full bg-kiosk-bg text-kiosk-ink font-tc box-border overflow-hidden pt-7 px-8 pb-8 flex flex-col gap-5 max-[920px]:overflow-y-auto max-[640px]:p-4 max-[640px]:gap-4">
    <!-- Top bar -->
    <div class="flex justify-between items-center px-1 shrink-0 max-[640px]:items-start max-[640px]:gap-4 max-[520px]:flex-col">
      <div>
        <div class="text-base text-kiosk-muted font-medium mb-1">目前站牌</div>
        <div class="text-[40px] font-extrabold text-kiosk-ink tracking-[-0.02em] leading-none">{{ snapshot?.stopName ?? "公車站牌" }}</div>
      </div>
      <div class="shrink-0">
        <div class="text-base text-kiosk-muted font-medium mb-1 text-right max-[520px]:text-left">現在時間</div>
        <div class="text-[40px] font-bold text-kiosk-ink tabular-nums tracking-[-0.02em] leading-none font-mono">{{ nowText }}</div>
      </div>
    </div>

    <!-- Background error banner — refetch failed but cached data still shown -->
    <div
      v-if="hasBackgroundError"
      class="shrink-0 flex items-center gap-2.5 px-5 py-3 rounded-2xl text-base font-semibold bg-amber-50 text-amber-800 border border-amber-200"
    >
      <WifiOff class="size-5 shrink-0" :stroke-width="2.2" />
      <span>目前無法更新資料，顯示最後紀錄<span v-if="lastUpdatedText">（{{ lastUpdatedText }}）</span></span>
    </div>

    <!-- First load: full-screen spinner -->
    <div v-if="isLoading && !snapshot" class="flex-1 flex items-center justify-center">
      <div class="flex items-center gap-3 text-lg font-semibold text-kiosk-muted">
        <LoaderCircle class="size-7 animate-spin" color="#6E685D" :stroke-width="2.2" />
        <span>載入本站資訊…</span>
      </div>
    </div>

    <!-- First load: full-screen error (no cached data) -->
    <div v-else-if="errorMessage && !snapshot" class="flex-1 flex items-center justify-center">
      <div class="flex flex-col items-center gap-3 text-center">
        <WifiOff class="size-10 text-kiosk-err" :stroke-width="2" />
        <div class="text-lg font-semibold text-kiosk-err">{{ errorMessage }}</div>
        <div class="text-base text-kiosk-muted">請確認網路連線，系統會自動重試</div>
      </div>
    </div>

    <div v-else class="flex-1 grid grid-cols-[minmax(0,1.1fr)_minmax(340px,1fr)] gap-5 min-h-0 max-[920px]:grid-cols-[minmax(0,1fr)] max-[920px]:auto-rows-auto max-[920px]:overflow-visible">
      <NoServiceHeroCard v-if="isAllClosed" />
      <DepartureHeroCard v-else :next-best="nextBest" />

      <div class="bg-white border-2 border-kiosk-line rounded-[32px] pt-5 px-5 pb-4 flex flex-col min-h-0 max-[920px]:min-h-[560px]">
        <RouteDetailPanel
          v-if="selectedRoute"
          :route="selectedRoute"
          :route-colors="routeColorAssignments"
          @back="selectedRoute = null"
        />
        <RouteList
          v-else
          :routes="routes"
          :is-loading="isLoading"
          :route-colors="routeColorAssignments"
          @select="selectedRoute = $event"
        />
      </div>
    </div>
  </div>
</template>
