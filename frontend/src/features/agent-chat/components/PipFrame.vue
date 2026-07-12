<script setup lang="ts">
import { computed, nextTick, ref, watch } from "vue"
import { LogOut, Settings } from "@lucide/vue"

import type { TtsState } from "../composables/useTts"
import type { ConversationState, PipCorner, PipSize } from "../types"
import Live2DAvatar from "./Live2DAvatar.vue"
import PipEndConfirmOverlay from "./PipEndConfirmOverlay.vue"
import PipIdleWarningOverlay from "./PipIdleWarningOverlay.vue"
import PipMoveOverlay from "./PipMoveOverlay.vue"
import PipSettingsOverlay from "./PipSettingsOverlay.vue"

const props = defineProps<{
  width: number
  height: number
  size: PipSize
  lastAgentText: string
  isSending: boolean
  showChat: boolean
  conversationState: ConversationState
  ttsState: TtsState
  mouthAmplitude: number
  moveMode: boolean
  settingsMode: boolean
  corner: PipCorner
  lastUserText?: string
  showEndConfirm: boolean
  endConfirmSecondsLeft: number
  showIdleWarning: boolean
  idleWarnSecondsLeft: number
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
  "toggle-settings": []
  "cancel-settings": []
  "set-size": [size: PipSize]
  "enter-move": []
  "open-chat": []
  "select-corner": [corner: PipCorner]
  "cancel-move": []
  "confirm-end": []
  "continue-conversation": []
  "dismiss-idle-warning": []
}>()

// Border + animation per conversation phase — three active states (userSpeaking /
// processing+thinking / speaking) each get a visually distinct treatment so the
// phase is legible at a glance. connecting/listening are static (no animation).
const borderClass = computed(() => {
  switch (props.conversationState) {
    case "connecting":
      return "border-[2px] border-kiosk-line2/70 shadow-[0_24px_48px_rgba(0,0,0,0.22),0_4px_12px_rgba(0,0,0,0.10)]"
    case "listening":
      return "border-[2px] border-kiosk-accent/50"
    case "userSpeaking":
      return "border-[2px] border-kiosk-warn pip-pulse-warm"
    case "processing":
    case "thinking":
      return "border-[2px] border-kiosk-accent pip-pulse-accent"
    case "speaking":
      return "border-[2px] border-kiosk-accent"
    case "error":
      // Connection dropped — static, no animation; the chip explains the exit.
      return "border-[2px] border-kiosk-err/60"
  }
})

// Short status chip text for phases that aren't otherwise self-explanatory.
// listening (idle, quiet) and speaking (Live2D mouth + subtitle bubble handle
// feedback already) intentionally show nothing here.
const statusChipText = computed(() => {
  switch (props.conversationState) {
    case "connecting": return "連線中…"
    case "userSpeaking": return "我咧聽…"
    case "processing": return "辨識中…"
    case "error": return "連線斷去，請按結束閣開一擺"
    default: return null
  }
})
</script>

<template>
  <div
    class="bg-kiosk-ink rounded-[22px] overflow-hidden relative shrink-0 transition-[border-color] duration-300"
    :class="borderClass"
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

    <!-- End conversation — top-right, labeled; owns the close behavior (replaces the old X) -->
    <button
      class="absolute top-2 right-2 z-[5] h-8 px-3 bg-white/20 text-white border-0 rounded-full cursor-pointer p-0 inline-flex items-center gap-1 backdrop-blur-sm text-[12px] font-bold"
      aria-label="結束對話"
      @click="emit('close')"
    >
      <LogOut class="size-[13px]" :stroke-width="2.4" />
      結束對話
    </button>

    <!-- Avatar — fills entire frame, button overlays on top -->
    <div class="absolute inset-0 z-[1]">
      <Live2DAvatar
        model-src="/live2d/ai-station/AI站長.model3.json"
        fallback-src="/avatar.png"
        :mouth-amplitude="mouthAmplitude"
      />
    </div>

    <!-- End-of-conversation confirm card -->
    <PipEndConfirmOverlay
      v-if="showEndConfirm"
      :seconds-left="endConfirmSecondsLeft"
      @end="emit('confirm-end')"
      @continue="emit('continue-conversation')"
    />

    <!-- Idle warning card -->
    <PipIdleWarningOverlay
      v-else-if="showIdleWarning"
      :seconds-left="idleWarnSecondsLeft"
      @dismiss="emit('dismiss-idle-warning')"
    />

    <!-- Move overlay -->
    <PipMoveOverlay
      v-else-if="moveMode"
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
        <!-- Status chip: connecting / userSpeaking / processing / error.
             Active phases carry a small motion cue (bars / dots) so the chip
             feels alive without competing with the Live2D avatar. -->
        <div
          v-if="statusChipText"
          class="bg-white/95 backdrop-blur-md border-2 border-kiosk-ink/10 py-2.5 px-4 rounded-[18px] shadow-[0_8px_24px_rgba(0,0,0,0.15)] flex items-center justify-center gap-2.5 min-w-[120px]"
        >
          <div v-if="conversationState === 'userSpeaking'" class="flex items-center gap-[3px] h-3.5">
            <span v-for="i in 3" :key="i" class="voice-bar" :style="`animation-delay:${(i - 1) * 0.15}s`" />
          </div>
          <template v-else-if="conversationState === 'processing'">
            <span v-for="i in 3" :key="i" class="pip-dot !bg-kiosk-accent !w-1.5 !h-1.5" :style="`animation-delay:${(i - 1) * 0.18}s`" />
          </template>
          <span class="text-kiosk-ink text-[14px] font-bold">{{ statusChipText }}</span>
        </div>

        <!-- Thinking indicator: REST text-send fallback OR voice 'thinking' phase -->
        <div
          v-else-if="(isSending || conversationState === 'thinking' || ttsState === 'loading') && !lastAgentText"
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
          v-else-if="lastUserText && !statusChipText"
          class="bg-white/95 backdrop-blur-md border-2 border-kiosk-ink/10 py-3 px-4 rounded-[18px] shadow-[0_8px_24px_rgba(0,0,0,0.15)] w-full max-h-[100px] overflow-y-auto pointer-events-auto"
        >
          <div class="text-kiosk-ink/70 text-[15px] font-bold leading-[1.45] text-center">「{{ lastUserText }}」</div>
        </div>
      </div>


    </template>
  </div>
</template>
