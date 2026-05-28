<script setup lang="ts">
import { Maximize2, MessageSquareText, Move, X } from "@lucide/vue"

import type { PipCorner, PipSize } from "../types"
import Live2DAvatar from "./Live2DAvatar.vue"
import PipMoveOverlay from "./PipMoveOverlay.vue"

defineProps<{
  width: number
  height: number
  size: PipSize
  lastAgentText: string
  showChat: boolean
  moveMode: boolean
  corner: PipCorner
}>()

const emit = defineEmits<{
  close: []
  "toggle-chat": []
  "enter-move": []
  "cycle-size": []
  "select-corner": [corner: PipCorner]
  "cancel-move": []
}>()

// h-11 (44px) + pt-2 (8px) + pb-2.5 (10px) = 62px
const BUTTON_AREA_HEIGHT = 62

const controlButtons = [
  { label: "移動", icon: Move, event: "enter-move" as const },
  { label: "大小", icon: Maximize2, event: "cycle-size" as const },
]
</script>

<template>
  <div
    class="bg-kiosk-ink rounded-[22px] overflow-hidden relative shadow-[0_24px_48px_rgba(0,0,0,0.22),0_4px_12px_rgba(0,0,0,0.10)] border-[2px] border-kiosk-line2/70 shrink-0"
    :style="{ width: `${width}px`, height: `${height}px` }"
  >
    <!-- Close button -->
    <button
      class="absolute top-2 right-2 z-[3] w-8 h-8 bg-white/20 text-white border-0 rounded-full cursor-pointer p-0 inline-flex items-center justify-center backdrop-blur-sm"
      aria-label="關閉"
      @click="emit('close')"
    >
      <X class="size-[18px]" :stroke-width="2.6" />
    </button>

    <!-- Avatar canvas — constrained to not extend under button row -->
    <div class="absolute top-0 left-0 right-0" :style="{ bottom: `${BUTTON_AREA_HEIGHT}px` }">
      <Live2DAvatar
        model-src="/live2d/ai-station/AI站長.model3.json"
        fallback-src="/avatar.png"
        :last-agent-text="lastAgentText"
      />
    </div>

    <!-- Last agent text — hidden on sm to avoid covering avatar -->
    <div
      v-if="size !== 'sm'"
      class="absolute left-2.5 right-2.5 bottom-16 bg-white/15 backdrop-blur-md py-2.5 px-3 rounded-xl z-[2]"
    >
      <div class="text-white text-[13px] font-semibold leading-[1.45] line-clamp-2">{{ lastAgentText }}</div>
    </div>

    <PipMoveOverlay
      v-if="moveMode"
      :corner="corner"
      @select="emit('select-corner', $event)"
      @cancel="emit('cancel-move')"
    />

    <div v-else class="absolute left-0 right-0 bottom-0 pt-2 px-2 pb-2.5 flex gap-1.5 z-[2]">
      <!-- Chat toggle — active state uses accent colour -->
      <button
        class="flex-1 h-11 border rounded-[14px] text-[13px] font-bold cursor-pointer font-[inherit] flex flex-col items-center justify-center gap-0.5 p-0"
        :class="showChat ? 'bg-kiosk-accent text-white border-kiosk-accent' : 'bg-white/15 text-white border-white/20'"
        @click="emit('toggle-chat')"
      >
        <MessageSquareText class="size-[18px]" :stroke-width="2.2" />
        <span class="text-xs font-bold leading-none">對話</span>
      </button>

      <!-- Static control buttons -->
      <button
        v-for="btn in controlButtons"
        :key="btn.label"
        class="flex-1 h-11 bg-white/15 text-white border border-white/20 rounded-[14px] text-[13px] font-bold cursor-pointer font-[inherit] flex flex-col items-center justify-center gap-0.5 p-0"
        @click="emit(btn.event)"
      >
        <component :is="btn.icon" class="size-[18px]" :stroke-width="2.2" />
        <span class="text-xs font-bold leading-none">{{ btn.label }}</span>
      </button>
    </div>
  </div>
</template>
