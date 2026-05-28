<script setup lang="ts">
import { Moon, Route } from "@lucide/vue"
import { computed } from "vue"
import { useRouter } from "vue-router"

import { usePip } from "@/features/agent-chat/composables/usePip"
import { formatTaipeiHourMinute } from "@/lib/time"
import { useNow } from "@/lib/useNow"

const props = defineProps<{ tomorrowFirstTime: string | null }>()

const router = useRouter()
const { open: openPip } = usePip()
const { now } = useNow()

/**
 * Minutes remaining until tomorrow's first bus.
 * Uses the Taipei-formatted current time so the countdown is
 * correct regardless of the browser's local timezone.
 */
const countdown = computed(() => {
  if (!props.tomorrowFirstTime) return null
  const [fh, fm] = props.tomorrowFirstTime.split(":").map(Number)
  if (isNaN(fh) || isNaN(fm)) return null

  const nowText = formatTaipeiHourMinute(now.value) // "HH:MM"
  const [nh, nm] = nowText.split(":").map(Number)
  let diff = fh * 60 + fm - (nh * 60 + nm)
  if (diff <= 0) diff += 24 * 60

  return { hours: Math.floor(diff / 60), minutes: diff % 60 }
})
</script>

<template>
  <div class="bg-white border-2 border-kiosk-ink rounded-[32px] px-7 py-6 flex flex-col min-h-0 max-[920px]:min-h-[560px]">
    <!-- Top row: status badge + moon icon -->
    <div class="flex justify-between items-center mb-5 shrink-0">
      <div class="py-2 px-5 bg-kiosk-ink text-kiosk-bg rounded-full text-lg font-bold inline-flex items-center gap-2">
        <span class="w-2.5 h-2.5 rounded-full bg-kiosk-bg/50" />
        今日已收班
      </div>
      <Moon class="size-7 text-kiosk-muted" :stroke-width="1.8" />
    </div>

    <!-- Headline -->
    <div class="shrink-0">
      <div class="text-[64px] font-extrabold text-kiosk-ink tracking-[-0.04em] leading-[0.9]">
        今日已無班次
      </div>
      <div class="mt-3 text-xl font-medium text-kiosk-muted">
        所有路線末班車均已離站
      </div>
    </div>

    <div class="h-0.5 bg-kiosk-line my-5 rounded shrink-0" />

    <!-- Tomorrow first bus + countdown -->
    <div class="flex items-end gap-5 shrink-0">
      <div class="flex-1 min-w-0">
        <div class="text-base text-kiosk-muted font-medium mb-1">明日首班</div>
        <div class="text-[54px] font-bold text-kiosk-ink tabular-nums tracking-[-0.03em] leading-none font-mono">
          {{ tomorrowFirstTime ?? "—" }}
        </div>
      </div>
      <div v-if="countdown" class="shrink-0 text-right pb-1">
        <div class="text-base text-kiosk-muted font-medium mb-1">還要等</div>
        <div class="inline-flex items-baseline gap-1 text-kiosk-ink font-bold">
          <span class="text-[38px] tabular-nums font-mono tracking-[-0.03em] leading-none">{{ countdown.hours }}</span>
          <span class="text-xl">小時</span>
          <span class="text-[38px] tabular-nums font-mono tracking-[-0.03em] leading-none">{{ countdown.minutes }}</span>
          <span class="text-xl">分</span>
        </div>
      </div>
    </div>

    <!-- Action buttons -->
    <div class="mt-auto pt-5 grid grid-cols-[repeat(auto-fit,minmax(220px,1fr))] gap-3 shrink-0">
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
          <span class="text-[13px] font-medium text-kiosk-muted leading-[1.2]">規劃明日行程</span>
          <span class="text-[22px] font-extrabold leading-[1.2] tracking-[-0.01em] text-kiosk-ink">查看路線</span>
        </span>
        <span class="text-[26px] font-normal text-kiosk-accent ml-auto">→</span>
      </button>
    </div>
  </div>
</template>
