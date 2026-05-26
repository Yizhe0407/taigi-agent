<script setup lang="ts">
import { ChevronRight, LoaderCircle, Route } from "@lucide/vue"
import { computed, ref } from "vue"
import { useRouter } from "vue-router"

import { usePip } from "@/features/agent-chat/composables/usePip"
import { formatTaipeiHourMinute } from "@/lib/time"
import { useNow } from "@/lib/useNow"

import { useRouteColors } from "@/lib/useRouteColors"
import { useDepartureSnapshot } from "../composables/useDepartureSnapshot"
import type { DepartureRouteStatus } from "../types"
import {
  departureDisplayState,
  departureMinutesLabel,
  heroStatusState,
  heroStatusText,
} from "../utils/departure-status"
import RouteDetailPanel from "./RouteDetailPanel.vue"

const router = useRouter()
const { open: openPip } = usePip()

const selectedRoute = ref<DepartureRouteStatus | null>(null)
const { snapshot, isLoading, errorMessage, routes, nextBest } =
  useDepartureSnapshot()
const { now } = useNow()
const nowText = computed(() => formatTaipeiHourMinute(now.value))
const { assignments: routeColorAssignments, getRouteBgClass } = useRouteColors(
  computed(() => routes.value.map((route) => route.route)),
)

/** Map departure status → Tailwind classes for chip + dot. Single source of truth so hero & route list stay in sync. */
function statusClasses(state: "boarding" | "pending" | "expired") {
  if (state === "boarding") return { chip: "bg-kiosk-ok-soft text-kiosk-ok", dot: "bg-kiosk-ok" }
  if (state === "pending") return { chip: "bg-kiosk-info-soft text-kiosk-info", dot: "bg-kiosk-info" }
  return { chip: "bg-kiosk-dim text-kiosk-faded", dot: "bg-kiosk-faded" }
}
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

    <!-- Loading placeholder -->
    <div v-if="isLoading && !snapshot" class="flex-1 flex items-center justify-center">
      <div class="flex items-center gap-3 text-lg font-semibold text-kiosk-muted">
        <LoaderCircle class="size-7 animate-spin" color="#6E685D" :stroke-width="2.2" />
        <span>載入本站資訊…</span>
      </div>
    </div>

    <!-- Error -->
    <div v-else-if="errorMessage && !snapshot" class="flex-1 flex items-center justify-center">
      <div class="text-lg font-semibold text-kiosk-err">{{ errorMessage }}</div>
    </div>

    <!-- Main grid -->
    <div v-else class="flex-1 grid grid-cols-[minmax(0,1.1fr)_minmax(340px,1fr)] gap-5 min-h-0 max-[920px]:grid-cols-[minmax(0,1fr)] max-[920px]:auto-rows-auto max-[920px]:overflow-visible">
      <!-- Left: Hero card -->
      <div class="bg-white border-2 border-kiosk-ink rounded-[32px] px-7 py-6 flex flex-col min-h-0 max-[920px]:min-h-[560px]">
        <div class="flex justify-between items-center mb-1 shrink-0">
          <div class="py-2 px-5 bg-kiosk-ink text-kiosk-bg rounded-full text-lg font-bold">下一班</div>
          <div
            v-if="nextBest && nextBest.minutes !== null"
            class="py-2 px-[18px] bg-kiosk-accent-soft rounded-full text-lg font-bold text-kiosk-accent inline-flex items-baseline gap-1.5"
          >
            <span class="text-2xl tabular-nums">{{ nextBest.minutes <= 0 ? '即將' : nextBest.minutes }}</span>
            <span class="text-base">{{ nextBest.minutes <= 0 ? '到站' : '分鐘後' }}</span>
          </div>
        </div>

        <div
          class="font-extrabold text-kiosk-accent tracking-[-0.06em] leading-[0.85] tabular-nums mt-1 shrink-0"
          :class="nextBest && nextBest.route.length > 3 ? 'text-[110px]' : 'text-[170px]'"
        >
          {{ nextBest?.route ?? "—" }}
        </div>
        <div class="flex items-baseline gap-3.5 mt-2.5 shrink-0">
          <span class="text-2xl text-kiosk-muted font-medium">往</span>
          <span class="text-[44px] font-extrabold text-kiosk-ink tracking-[-0.02em]">{{ nextBest?.direction ?? "無可搭班次" }}</span>
        </div>

        <div class="h-0.5 bg-kiosk-line my-4 rounded shrink-0" />

        <div class="flex justify-between items-end mb-4 shrink-0">
          <div>
            <div class="text-base text-kiosk-muted font-medium mb-1">預定發車</div>
            <div class="text-[54px] font-bold text-kiosk-ink tabular-nums tracking-[-0.03em] leading-none font-mono">
              {{ nextBest?.scheduledTime ?? (nextBest?.statusText ?? "—") }}
            </div>
          </div>
          <div
            class="inline-flex items-center gap-2.5 py-2.5 px-[18px] rounded-full text-lg font-bold"
            :class="statusClasses(heroStatusState(nextBest)).chip"
          >
            <span
              class="w-3 h-3 rounded-full"
              :class="statusClasses(heroStatusState(nextBest)).dot"
            />
            {{ heroStatusText(nextBest) }}
          </div>
        </div>

        <div class="mt-auto grid grid-cols-[repeat(auto-fit,minmax(220px,1fr))] gap-3 shrink-0">
          <!-- Ask virtual agent -->
          <button
            class="w-full min-w-0 h-[76px] pl-2.5 pr-[22px] bg-kiosk-ink text-white border-2 border-kiosk-ink rounded-[22px] font-[inherit] cursor-pointer inline-flex items-center gap-3.5 overflow-hidden [&>*]:pointer-events-none"
            @click="openPip"
          >
            <span class="w-14 h-14 rounded-full bg-kiosk-accent overflow-hidden inline-flex items-center justify-center border-2 border-kiosk-accent shrink-0">
              <img src="/avatar.png" alt="" class="w-full h-full object-cover object-[center_12%]" />
            </span>
            <span class="flex flex-col items-start text-left min-w-0">
              <span class="text-[13px] font-medium text-white/70 leading-[1.2]">需要幫忙嗎？</span>
              <span class="text-[22px] font-extrabold leading-[1.2] tracking-[-0.01em]">讓小芸幫您</span>
            </span>
            <span class="text-[26px] font-normal text-kiosk-accent ml-auto">→</span>
          </button>

          <!-- Route planner -->
          <button
            class="w-full min-w-0 h-[76px] pl-3 pr-[22px] bg-white text-kiosk-ink border-2 border-kiosk-ink rounded-[22px] font-[inherit] cursor-pointer inline-flex items-center gap-3.5 overflow-hidden [&>*]:pointer-events-none"
            @click="router.push('/plan')"
          >
            <span class="w-14 h-14 rounded-full bg-kiosk-accent-soft text-kiosk-accent inline-flex items-center justify-center shrink-0">
              <Route class="size-7" :stroke-width="2.2" />
            </span>
            <span class="flex flex-col items-start text-left min-w-0">
              <span class="text-[13px] font-medium text-kiosk-muted leading-[1.2]">想去某個地方？</span>
              <span class="text-[22px] font-extrabold leading-[1.2] tracking-[-0.01em] text-kiosk-ink">規劃路線</span>
            </span>
            <span class="text-[26px] font-normal text-kiosk-accent ml-auto">→</span>
          </button>
        </div>
      </div>

      <!-- Right: route list OR route detail -->
      <div class="bg-white border-2 border-kiosk-line rounded-[32px] pt-5 px-5 pb-4 flex flex-col min-h-0 max-[920px]:min-h-[560px]">
        <!-- Route detail -->
        <RouteDetailPanel
          v-if="selectedRoute"
          :route="selectedRoute"
          :route-colors="routeColorAssignments"
          @back="selectedRoute = null"
        />

        <!-- Route list -->
        <template v-else>
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
              @click="selectedRoute = route"
            >
              <div
                class="col-start-1 row-start-1 justify-self-center w-[60px] h-[60px] rounded-full text-white text-lg font-extrabold tracking-[-0.02em] tabular-nums inline-flex items-center justify-center shrink-0 box-border overflow-hidden px-1 text-ellipsis whitespace-nowrap max-[1180px]:row-span-2 max-[420px]:w-[54px] max-[420px]:h-[54px] max-[420px]:text-base"
                :class="getRouteBgClass(route.route)"
              >{{ route.route }}</div>

              <div class="col-start-2 row-start-1 grid min-w-0 grid-cols-[auto_minmax(0,1fr)] items-center gap-2">
                <div class="justify-self-end whitespace-nowrap text-base text-kiosk-muted font-medium">往</div>
                <div class="min-w-0 overflow-hidden [display:-webkit-box] [-webkit-box-orient:vertical] [-webkit-line-clamp:2] [overflow-wrap:anywhere] text-[22px] font-bold leading-[1.15] text-kiosk-ink max-[420px]:text-lg">{{ route.direction }}</div>
              </div>

              <div
                class="col-start-3 row-start-1 justify-self-center inline-flex min-w-[7.5rem] max-w-full items-center justify-center gap-2 whitespace-nowrap py-2 px-3.5 rounded-full text-base font-bold max-[1180px]:col-start-2 max-[1180px]:row-start-2 max-[1180px]:justify-self-start max-[1180px]:self-start max-[1180px]:min-w-0 max-[420px]:py-1.5 max-[420px]:px-2.5 max-[420px]:text-sm max-[340px]:col-start-1 max-[340px]:col-span-2 max-[340px]:justify-self-center"
                :class="statusClasses(departureDisplayState(route)).chip"
              >
                <span
                  class="w-2.5 h-2.5 rounded-full"
                  :class="statusClasses(departureDisplayState(route)).dot"
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
      </div>
    </div>
  </div>
</template>
