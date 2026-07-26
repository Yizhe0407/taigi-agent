<script setup lang="ts">
// Isolated on purpose: this is the only piece of the departures dashboard that
// needs a once-a-second tick. Keeping the ticker here (instead of on the
// shared snapshot composable) means only this small badge re-renders every
// second — not the whole RouteList (and its v-for over every route card).
import { RefreshCw } from "@lucide/vue"
import { computed } from "vue"

import { useNow } from "@/lib/useNow"

const props = defineProps<{
  /** `query.dataUpdatedAt` from useDepartureSnapshot — ms epoch of the last fetch. */
  dataUpdatedAt: number
  /** Expected refresh cadence (SSE push or poll interval), in ms. */
  intervalMs: number
}>()

const { now } = useNow()

const secondsLeft = computed(() => {
  const elapsed = now.value.getTime() - props.dataUpdatedAt
  return Math.max(0, Math.round((props.intervalMs - elapsed) / 1000))
})
</script>

<template>
  <div class="flex items-center gap-1.5 text-lg text-kiosk-muted font-medium">
    <RefreshCw class="size-[18px] shrink-0" :stroke-width="2.2" />
    <span class="font-mono tabular-nums">{{ secondsLeft }}s</span>
  </div>
</template>
