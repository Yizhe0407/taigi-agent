<script setup lang="ts">
import { nextTick, useTemplateRef, watch } from "vue"

import { HOURS, MINUTES } from "../composables/useScheduledTimeWheel"

const props = defineProps<{
  open: boolean
  pendingHour: number
  pendingMinute: number
}>()

defineEmits<{
  "update:open": [open: boolean]
  "hour-scroll": [event: Event]
  "minute-scroll": [event: Event]
  confirm: []
}>()

const pad = (n: number) => String(n).padStart(2, "0")
const WHEEL_ITEM_HEIGHT = 56
const hourScrollEl = useTemplateRef<HTMLElement>("hour-wheel")
const minuteScrollEl = useTemplateRef<HTMLElement>("minute-wheel")

watch(
  () => [props.open, props.pendingHour, props.pendingMinute] as const,
  async ([open]) => {
    if (!open) return
    await nextTick()

    if (hourScrollEl.value) {
      hourScrollEl.value.scrollTop = props.pendingHour * WHEEL_ITEM_HEIGHT
    }
    if (minuteScrollEl.value) {
      const minuteIndex = MINUTES.indexOf(props.pendingMinute)
      minuteScrollEl.value.scrollTop =
        Math.max(0, minuteIndex) * WHEEL_ITEM_HEIGHT
    }
  },
)
</script>

<template>
  <Teleport to="body">
    <Transition
      enter-active-class="transition-opacity duration-200 ease-out [&_.sheet]:transition-transform [&_.sheet]:duration-[250ms] [&_.sheet]:ease-[cubic-bezier(0.32,0,0.67,0)]"
      enter-from-class="opacity-0 [&_.sheet]:translate-y-full"
      leave-active-class="transition-opacity duration-200 ease-out [&_.sheet]:transition-transform [&_.sheet]:duration-[250ms] [&_.sheet]:ease-[cubic-bezier(0.32,0,0.67,0)]"
      leave-to-class="opacity-0 [&_.sheet]:translate-y-full"
    >
      <div
        v-if="open"
        class="sheet-overlay fixed inset-0 bg-kiosk-ink/45 z-[300] flex items-end justify-center"
        @click="$emit('update:open', false)"
      >
        <div
          class="sheet w-full max-w-[560px] bg-white rounded-t-[28px] pt-2.5 px-7 pb-8 shadow-[0_-24px_48px_rgba(0,0,0,0.18)] flex flex-col gap-4 font-tc"
          @click.stop
        >
          <div class="w-11 h-[5px] bg-kiosk-line rounded-full self-center mb-1" />
          <div class="text-center">
            <div class="text-[22px] font-extrabold text-kiosk-ink tracking-[-0.01em]">指定出發時間</div>
            <div class="text-[13px] text-kiosk-muted font-medium mt-1">滑動下方數字選擇時刻</div>
          </div>
          <div class="flex items-center justify-center gap-2.5 pt-1 pb-2">
            <div class="relative w-[110px] h-[280px] bg-white rounded-[18px] overflow-hidden border-2 border-kiosk-line">
              <div class="absolute top-[112px] left-1.5 right-1.5 h-14 bg-kiosk-accent-soft rounded-xl pointer-events-none z-0" />
              <div class="absolute top-0 left-0 right-0 h-14 pointer-events-none z-[2] bg-white/80" />
              <div class="absolute bottom-0 left-0 right-0 h-14 pointer-events-none z-[2] bg-white/80" />
              <div
                ref="hour-wheel"
                class="h-full overflow-y-auto [scroll-snap-type:y_mandatory] relative z-[1]"
                @scroll="$emit('hour-scroll', $event)"
              >
                <div class="h-[112px]" />
                <div
                  v-for="h in HOURS"
                  :key="h"
                  class="h-14 [scroll-snap-align:center] flex items-center justify-center tabular-nums tracking-[-0.02em] font-mono"
                  :class="h === pendingHour
                    ? 'text-[36px] font-extrabold text-kiosk-ink'
                    : 'text-[28px] font-semibold text-kiosk-mute2'"
                >
                  {{ pad(h) }}
                </div>
                <div class="h-[112px]" />
              </div>
            </div>

            <div class="text-[44px] font-bold text-kiosk-ink tabular-nums px-2 leading-none font-mono">:</div>

            <div class="relative w-[110px] h-[280px] bg-white rounded-[18px] overflow-hidden border-2 border-kiosk-line">
              <div class="absolute top-[112px] left-1.5 right-1.5 h-14 bg-kiosk-accent-soft rounded-xl pointer-events-none z-0" />
              <div class="absolute top-0 left-0 right-0 h-14 pointer-events-none z-[2] bg-white/80" />
              <div class="absolute bottom-0 left-0 right-0 h-14 pointer-events-none z-[2] bg-white/80" />
              <div
                ref="minute-wheel"
                class="h-full overflow-y-auto [scroll-snap-type:y_mandatory] relative z-[1]"
                @scroll="$emit('minute-scroll', $event)"
              >
                <div class="h-[112px]" />
                <div
                  v-for="m in MINUTES"
                  :key="m"
                  class="h-14 [scroll-snap-align:center] flex items-center justify-center tabular-nums tracking-[-0.02em] font-mono"
                  :class="m === pendingMinute
                    ? 'text-[36px] font-extrabold text-kiosk-ink'
                    : 'text-[28px] font-semibold text-kiosk-mute2'"
                >
                  {{ pad(m) }}
                </div>
                <div class="h-[112px]" />
              </div>
            </div>
          </div>

          <div class="grid grid-cols-[1fr_1.4fr] gap-2.5">
            <button
              class="h-[60px] bg-white border-2 border-kiosk-line text-kiosk-ink rounded-[20px] text-lg font-bold cursor-pointer font-tc"
              @click="$emit('update:open', false)"
            >取消</button>
            <button
              class="h-[60px] bg-kiosk-ink text-white border-2 border-kiosk-ink rounded-[20px] text-lg font-extrabold cursor-pointer font-mono tabular-nums tracking-[-0.01em]"
              @click="$emit('confirm')"
            >
              確認 · {{ pad(pendingHour) }}:{{ pad(pendingMinute) }}
            </button>
          </div>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>
