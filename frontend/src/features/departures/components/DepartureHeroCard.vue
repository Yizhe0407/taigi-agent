<script setup lang="ts">
import { Route } from "@lucide/vue"
import { useRouter } from "vue-router"

import { usePip } from "@/features/agent-chat/composables/usePip"

import type { DepartureRouteStatus } from "../types"
import {
  heroStatusState,
  heroStatusText,
  statusChipClasses,
} from "../utils/departure-status"

defineProps<{ nextBest: DepartureRouteStatus | null }>()

const router = useRouter()
const { open: openPip } = usePip()
</script>

<template>
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
        :class="statusChipClasses(heroStatusState(nextBest)).chip"
      >
        <span
          class="w-3 h-3 rounded-full"
          :class="statusChipClasses(heroStatusState(nextBest)).dot"
        />
        {{ heroStatusText(nextBest) }}
      </div>
    </div>

    <div class="mt-auto grid grid-cols-[repeat(auto-fit,minmax(220px,1fr))] gap-3 shrink-0">
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
</template>
