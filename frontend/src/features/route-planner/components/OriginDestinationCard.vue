<script setup lang="ts">
import type { LngLat } from "../types"

defineProps<{
  originName: string
  destination: LngLat | null
  destinationLabel: string | null
}>()

defineEmits<{ reset: [] }>()
</script>

<template>
  <div class="bg-kiosk-soft rounded-[20px] py-3.5 px-4 shrink-0">
    <div class="flex items-center gap-3.5">
      <div class="w-4 h-4 rounded-full shrink-0 ml-1 bg-kiosk-ink shadow-[0_0_0_4px_#FFFFFF]" />
      <div class="flex-1 min-w-0">
        <div class="text-xs text-kiosk-muted font-semibold mb-0.5">起點</div>
        <div class="text-[19px] font-extrabold text-kiosk-ink tracking-[-0.01em] truncate">{{ originName }}</div>
      </div>
    </div>
    <div class="w-0.5 h-4 bg-kiosk-line my-1 ml-[11px]" />
    <div class="flex items-center gap-3.5">
      <div class="w-4 h-4 rounded-full shrink-0 ml-1 bg-kiosk-accent shadow-[0_0_0_4px_#FFFFFF]" />
      <div class="flex-1 min-w-0">
        <div class="text-xs text-kiosk-muted font-semibold mb-0.5">目的地</div>
        <div
          class="text-[19px] tracking-[-0.01em] truncate"
          :class="destination
            ? 'text-kiosk-ink font-extrabold'
            : 'text-kiosk-faded font-semibold'"
        >
          {{ destinationLabel ?? "尚未選擇" }}
        </div>
      </div>
      <button
        v-if="destination"
        class="h-[34px] px-3.5 bg-white border-2 border-kiosk-ink rounded-full text-[13px] font-bold text-kiosk-ink font-[inherit] cursor-pointer shrink-0"
        @click="$emit('reset')"
      >更改</button>
    </div>
  </div>
</template>
