<script setup lang="ts">
import { nextTick, ref, watch } from "vue"
import { Mic, Settings, X } from "@lucide/vue"

import type { TtsState } from "../composables/useTts"
import type { VoiceState } from "../composables/useVoiceInput"
import type { PipCorner, PipSize } from "../types"
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
      <!-- Last agent text bubble — md/lg only -->
      <div
        v-if="size !== 'sm' && (lastAgentText || isSending || ttsState === 'loading')"
        class="absolute bottom-[72px] left-2.5 right-2.5 z-[2]"
      >
        <!-- Thinking indicator -->
        <div v-if="(isSending || ttsState === 'loading') && !lastAgentText" class="bg-white/15 backdrop-blur-md py-3.5 px-4 rounded-xl flex items-center gap-2.5">
          <span v-for="i in 3" :key="i" class="pip-dot" :style="`animation-delay:${(i - 1) * 0.18}s`" />
          <span class="text-white/80 text-[15px] font-semibold">小芸思考中…</span>
        </div>
        <!-- Text bubble -->
        <div v-else ref="bubbleRef" class="bg-white/15 backdrop-blur-md py-2.5 px-3 rounded-xl max-h-[120px] overflow-y-auto">
          <div class="text-white text-[16px] font-semibold leading-[1.45]">{{ lastAgentText }}</div>
        </div>
      </div>

      <!-- Voice pill button -->
      <button
        class="absolute bottom-3 left-3 right-3 h-[52px] z-[3] rounded-[16px] border-0 font-[inherit] flex items-center justify-center gap-2 transition-colors duration-200"
        :class="{
          'bg-red-500 text-white shadow-[0_4px_20px_rgba(239,68,68,0.45)] cursor-pointer': voiceState === 'recording',
          'bg-kiosk-accent text-white shadow-[0_4px_20px_rgba(216,106,31,0.45)] cursor-default': voiceState === 'transcribing',
          'bg-white/20 backdrop-blur-md text-white border border-white/30 cursor-pointer': voiceState === 'idle',
        }"
        :disabled="voiceState === 'transcribing'"
        @click="emit('toggle-voice')"
      >
        <template v-if="voiceState === 'recording'">
          <div class="flex items-end gap-[3px]">
            <span v-for="i in 4" :key="i" class="voice-bar" :style="`animation-delay: ${(i - 1) * 0.15}s`" />
          </div>
          <span class="text-[16px] font-bold">說完後再按一次</span>
        </template>
        <template v-else-if="voiceState === 'transcribing'">
          <span class="text-[16px] font-bold">辨識中…</span>
        </template>
        <template v-else-if="ttsState !== 'idle'">
          <div class="flex items-end gap-[3px]">
            <span v-for="i in 4" :key="i" class="voice-bar" :style="`animation-delay: ${(i - 1) * 0.15}s`" />
          </div>
          <span class="text-[16px] font-bold">說話中…</span>
        </template>
        <template v-else>
          <Mic class="size-[20px]" :stroke-width="2.2" />
          <span class="text-[16px] font-bold">點這裡說話</span>
        </template>
      </button>
    </template>
  </div>
</template>
