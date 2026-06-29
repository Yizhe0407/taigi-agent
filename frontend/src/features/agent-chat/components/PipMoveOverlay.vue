<script setup lang="ts">
import type { PipCorner } from "../types"

const CORNERS: PipCorner[] = ["tl", "tr", "bl", "br"]

defineProps<{ corner: PipCorner }>()

defineEmits<{
  select: [corner: PipCorner]
  cancel: []
}>()
</script>

<template>
  <div class="absolute inset-0 bg-kiosk-ink/90 z-[4] pt-[50px] px-4 pb-4 flex flex-col gap-3">
    <div class="text-white text-[13px] font-semibold text-center">請點選要移動到的位置</div>
    <div class="flex-1 grid grid-cols-2 grid-rows-2 gap-2">
      <button
        v-for="c in CORNERS"
        :key="c"
        class="rounded-xl cursor-pointer p-0 flex items-center justify-center"
        :class="c === corner
          ? 'bg-kiosk-accent border-2 border-kiosk-accent'
          : 'bg-white/[0.06] border-2 border-dashed border-white/30'"
        @click="$emit('select', c)"
      >
        <span class="w-3 h-3 rounded-full bg-white" />
      </button>
    </div>
    <button
      class="h-10 bg-white/[0.12] border-0 text-white rounded-full text-sm font-bold cursor-pointer font-[inherit]"
      @click="$emit('cancel')"
    >取消</button>
  </div>
</template>
