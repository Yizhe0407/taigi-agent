<script setup lang="ts">
import { Maximize2, MessageSquareText, Move, X } from "@lucide/vue"

import type { PipCorner } from "../types"
import PipMoveOverlay from "./PipMoveOverlay.vue"

defineProps<{
  width: number
  height: number
  lastAgentText: string
  showChat: boolean
  moveMode: boolean
  corner: PipCorner
}>()

defineEmits<{
  close: []
  "toggle-chat": []
  "enter-move": []
  "cycle-size": []
  "select-corner": [corner: PipCorner]
  "cancel-move": []
}>()
</script>

<template>
  <div
    class="bg-kiosk-ink rounded-[22px] overflow-hidden relative shadow-[0_24px_48px_rgba(0,0,0,0.20),0_4px_12px_rgba(0,0,0,0.10)] border-[3px] border-white shrink-0"
    :style="{ width: `${width}px`, height: `${height}px` }"
  >
    <!-- Top bar -->
    <div class="absolute top-0 left-0 right-0 py-2.5 px-3 bg-kiosk-ink/55 flex justify-between items-center z-[3]">
      <div class="flex items-center gap-2">
        <span class="w-2 h-2 rounded-full bg-kiosk-accent" />
        <span class="text-white text-sm font-bold">小芸</span>
      </div>
      <button
        class="w-8 h-8 bg-white/20 text-white border-0 rounded-full cursor-pointer p-0 inline-flex items-center justify-center"
        aria-label="關閉"
        @click="$emit('close')"
      >
        <X class="size-[18px]" :stroke-width="2.6" />
      </button>
    </div>

    <img
      src="/avatar.png"
      alt="虛擬站務員小芸"
      class="absolute inset-0 w-full h-full object-cover object-top"
    />

    <div class="absolute left-2.5 right-2.5 bottom-16 bg-kiosk-ink/80 py-2.5 px-3 rounded-xl z-[2]">
      <div class="text-white text-[13px] font-semibold leading-[1.45] line-clamp-2">{{ lastAgentText }}</div>
    </div>

    <PipMoveOverlay
      v-if="moveMode"
      :corner="corner"
      @select="$emit('select-corner', $event)"
      @cancel="$emit('cancel-move')"
    />

    <div v-else class="absolute left-0 right-0 bottom-0 pt-2 px-2 pb-2.5 flex gap-1.5 z-[2]">
      <button
        class="flex-1 h-11 border-0 rounded-[14px] text-[13px] font-bold cursor-pointer font-[inherit] flex flex-col items-center justify-center gap-0.5 p-0"
        :class="showChat ? 'bg-kiosk-accent text-white' : 'bg-white/[0.92] text-kiosk-ink'"
        @click="$emit('toggle-chat')"
      >
        <MessageSquareText class="size-[18px]" :stroke-width="2.2" />
        <span class="text-xs font-bold leading-none">對話</span>
      </button>
      <button
        class="flex-1 h-11 bg-white/[0.92] text-kiosk-ink border-0 rounded-[14px] text-[13px] font-bold cursor-pointer font-[inherit] flex flex-col items-center justify-center gap-0.5 p-0"
        @click="$emit('enter-move')"
      >
        <Move class="size-[18px]" :stroke-width="2.2" />
        <span class="text-xs font-bold leading-none">移動</span>
      </button>
      <button
        class="flex-1 h-11 bg-white/[0.92] text-kiosk-ink border-0 rounded-[14px] text-[13px] font-bold cursor-pointer font-[inherit] flex flex-col items-center justify-center gap-0.5 p-0"
        @click="$emit('cycle-size')"
      >
        <Maximize2 class="size-[18px]" :stroke-width="2.2" />
        <span class="text-xs font-bold leading-none">大小</span>
      </button>
    </div>
  </div>
</template>
