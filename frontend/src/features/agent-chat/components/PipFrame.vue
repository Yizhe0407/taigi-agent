<script setup lang="ts">
import { nextTick, ref, watch } from "vue"
import { Settings, X } from "@lucide/vue"

import type { TtsState } from "../composables/useTts"
import type { PipCorner, PipSize, VoiceState, WebRTCState } from "../types"
import Live2DAvatar from "./Live2DAvatar.vue"
import PipMoveOverlay from "./PipMoveOverlay.vue"
import PipSettingsOverlay from "./PipSettingsOverlay.vue"

const props = defineProps<{
  width: number
  height: number
  size: PipSize
  lastAgentText: string
  isSending: boolean
  showChat: boolean
  voiceState: VoiceState
  ttsState: TtsState
  mouthAmplitude: number
  moveMode: boolean
  settingsMode: boolean
  corner: PipCorner
  webrtcState?: WebRTCState | null
  lastUserText?: string
}>()

const bubbleRef = ref<HTMLElement | null>(null)

watch(
  () => props.lastAgentText,
  async () => {
    await nextTick()
    if (bubbleRef.value) bubbleRef.value.scrollTop = bubbleRef.value.scrollHeight
  },
)

const emit = defineEmits<{
  close: []
  "toggle-voice": []
  "toggle-settings": []
  "cancel-settings": []
  "set-size": [size: PipSize]
  "enter-move": []
  "open-chat": []
  "select-corner": [corner: PipCorner]
  "cancel-move": []
}>()

</script>

<template>
  <div
    class="bg-kiosk-ink rounded-[22px] overflow-hidden relative shrink-0 transition-[border-color] duration-300"
    :class="voiceState !== 'idle'
      ? 'border-[2px] border-kiosk-accent pip-listening'
      : 'border-[2px] border-kiosk-line2/70 shadow-[0_24px_48px_rgba(0,0,0,0.22),0_4px_12px_rgba(0,0,0,0.10)]'"
    :style="{ width: `${width}px`, height: `${height}px` }"
  >
    <!-- Settings — top-left -->
    <button
      class="absolute top-2 left-2 z-[5] w-8 h-8 bg-white/15 text-white/60 border-0 rounded-full cursor-pointer p-0 inline-flex items-center justify-center backdrop-blur-sm"
      aria-label="設定"
      @click="emit('toggle-settings')"
    >
      <Settings class="size-[15px]" :stroke-width="2" />
    </button>

    <!-- Close — top-right -->
    <button
      class="absolute top-2 right-2 z-[5] w-8 h-8 bg-white/20 text-white border-0 rounded-full cursor-pointer p-0 inline-flex items-center justify-center backdrop-blur-sm"
      aria-label="關閉"
      @click="emit('close')"
    >
      <X class="size-[18px]" :stroke-width="2.6" />
    </button>

    <!-- Avatar — fills entire frame, button overlays on top -->
    <div class="absolute inset-0 z-[1]">
      <Live2DAvatar
        model-src="/live2d/ai-station/AI站長.model3.json"
        fallback-src="/avatar.png"
        :mouth-amplitude="mouthAmplitude"
      />
    </div>

    <!-- Move overlay -->
    <PipMoveOverlay
      v-if="moveMode"
      :corner="corner"
      @select="emit('select-corner', $event)"
      @cancel="emit('cancel-move')"
    />

    <!-- Settings overlay -->
    <PipSettingsOverlay
      v-else-if="settingsMode"
      :size="size"
      @set-size="emit('set-size', $event)"
      @enter-move="emit('enter-move')"
      @open-chat="emit('open-chat')"
      @cancel="emit('cancel-settings')"
    />

    <template v-else>
      <!-- Bubble area — md/lg only -->
      <div
        v-if="size !== 'sm'"
        class="absolute bottom-5 left-2.5 right-2.5 z-[2] flex flex-col items-center gap-1.5 pointer-events-none"
      >
        <!-- Thinking indicator -->
        <div 
          v-if="(isSending || ttsState === 'loading') && !lastAgentText"
          class="bg-white/95 backdrop-blur-md border-2 border-kiosk-ink/10 py-2.5 px-4 rounded-[18px] shadow-[0_8px_24px_rgba(0,0,0,0.15)] flex items-center justify-center gap-2.5 min-w-[120px]"
        >
          <span v-for="i in 3" :key="i" class="pip-dot !bg-kiosk-accent !w-1.5 !h-1.5" :style="`animation-delay:${(i - 1) * 0.18}s`" />
          <span class="text-kiosk-ink text-[14px] font-bold">思考中</span>
        </div>

        <!-- Single Transcript Panel -->
        <div 
          v-if="lastAgentText" 
          ref="bubbleRef" 
          class="bg-white/95 backdrop-blur-md border-2 border-kiosk-ink/10 py-3 px-4 rounded-[18px] shadow-[0_8px_24px_rgba(0,0,0,0.15)] w-full max-h-[100px] overflow-y-auto pointer-events-auto"
        >
          <div class="text-kiosk-ink text-[15px] font-bold leading-[1.45] text-center">{{ lastAgentText }}</div>
        </div>
        <div 
          v-else-if="lastUserText && webrtcState != null"
          class="bg-white/95 backdrop-blur-md border-2 border-kiosk-ink/10 py-3 px-4 rounded-[18px] shadow-[0_8px_24px_rgba(0,0,0,0.15)] w-full max-h-[100px] overflow-y-auto pointer-events-auto"
        >
          <div class="text-kiosk-ink/70 text-[15px] font-bold leading-[1.45] text-center">「{{ lastUserText }}」</div>
        </div>
      </div>


    </template>
  </div>
</template>
