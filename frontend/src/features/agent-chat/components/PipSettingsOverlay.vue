<script setup lang="ts">
import { MessageSquareText, Move } from "@lucide/vue"

import type { PipSize } from "../types"

const SIZES: { key: PipSize; label: string }[] = [
  { key: "sm", label: "小" },
  { key: "md", label: "中" },
  { key: "lg", label: "大" },
]

defineProps<{ size: PipSize }>()

defineEmits<{
  "set-size": [size: PipSize]
  "enter-move": []
  "open-chat": []
  cancel: []
}>()
</script>

<template>
  <div class="absolute inset-0 bg-kiosk-ink/92 z-[4] pt-12 px-4 pb-4 flex flex-col gap-2.5 backdrop-blur-sm">
    <!-- Size selector -->
    <div class="flex gap-1.5">
      <button
        v-for="s in SIZES"
        :key="s.key"
        class="flex-1 h-10 rounded-[12px] text-[13px] font-bold cursor-pointer font-[inherit] border"
        :class="s.key === size
          ? 'bg-kiosk-accent text-white border-kiosk-accent'
          : 'bg-white/10 text-white border-white/20'"
        @click="$emit('set-size', s.key)"
      >
        {{ s.label }}
      </button>
    </div>

    <!-- Move + Text chat -->
    <button
      class="h-10 bg-white/10 text-white border border-white/20 rounded-[12px] text-[13px] font-bold cursor-pointer font-[inherit] flex items-center justify-center gap-1.5"
      @click="$emit('enter-move')"
    >
      <Move class="size-[16px]" :stroke-width="2.2" />
      移動位置
    </button>
    <button
      class="h-10 bg-white/10 text-white border border-white/20 rounded-[12px] text-[13px] font-bold cursor-pointer font-[inherit] flex items-center justify-center gap-1.5"
      @click="$emit('open-chat')"
    >
      <MessageSquareText class="size-[16px]" :stroke-width="2.2" />
      文字對話
    </button>

    <!-- Cancel -->
    <button
      class="h-9 bg-white/[0.07] border-0 text-white/60 rounded-full text-sm font-bold cursor-pointer font-[inherit] mt-auto"
      @click="$emit('cancel')"
    >
      取消
    </button>
  </div>
</template>
