<script setup lang="ts">
import type { DepartureMode } from "../composables/useScheduledTimeWheel"

defineProps<{
  departureMode: DepartureMode
  scheduledTimeDisplay: string | null
}>()

defineEmits<{
  "update:departureMode": [mode: DepartureMode]
  "open-sheet": []
}>()
</script>

<template>
  <div class="flex items-center justify-between gap-3 shrink-0">
    <span class="text-sm text-kiosk-muted font-semibold">出發時間</span>
    <div class="inline-flex bg-kiosk-soft p-1 rounded-full gap-0.5">
      <button
        class="h-10 px-[18px] border-0 rounded-full text-sm font-bold font-[inherit] cursor-pointer inline-flex items-center gap-1"
        :class="departureMode === 'now'
          ? 'bg-kiosk-ink text-white'
          : 'bg-transparent text-kiosk-muted'"
        @click="$emit('update:departureMode', 'now')"
      >
        現在出發
      </button>
      <button
        class="h-10 px-[18px] border-0 rounded-full text-sm font-bold font-[inherit] cursor-pointer inline-flex items-center gap-1"
        :class="departureMode === 'scheduled'
          ? 'bg-kiosk-ink text-white'
          : 'bg-transparent text-kiosk-muted'"
        @click="$emit('open-sheet')"
      >
        <template v-if="scheduledTimeDisplay && departureMode === 'scheduled'">
          指定 {{ scheduledTimeDisplay }}
          <span class="text-xs opacity-80">✎</span>
        </template>
        <template v-else>指定時間</template>
      </button>
    </div>
  </div>
</template>
