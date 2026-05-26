<script setup lang="ts">
import {
  Maximize2,
  MessageSquareText,
  Move,
  Send,
  X,
} from "@lucide/vue"
import { computed, ref, toRef, watch } from "vue"

import { usePipChat } from "@/features/agent-chat/composables/usePipChat"

type Corner = "tl" | "tr" | "bl" | "br"
type Size = "sm" | "md" | "lg"

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ close: [] }>()

const corner = ref<Corner>("br")
const size = ref<Size>("lg")
const moveMode = ref(false)
const {
  messages,
  userInput,
  isSending,
  showChat,
  lastAgentText,
  sendMessage,
  handleKeydown,
  toggleChat,
} = usePipChat(toRef(props, "open"))

watch(
  () => props.open,
  (open) => {
    showChat.value = open
    if (!open) moveMode.value = false
  },
)

const SIZES: Record<Size, { w: number; h: number }> = {
  sm: { w: 220, h: 275 },
  md: { w: 280, h: 350 },
  lg: { w: 340, h: 425 },
}

function cycleSize() {
  size.value = size.value === "sm" ? "md" : size.value === "md" ? "lg" : "sm"
}

// Class-based positioning — Tailwind utilities derived from corner. Avoids inline-style reactivity issues with Transition.
const posClass = computed(() => {
  switch (corner.value) {
    case "tl": return "top-5 left-5"
    case "tr": return "top-5 right-5"
    case "bl": return "bottom-5 left-5"
    case "br": return "bottom-5 right-5"
  }
})
const dirClass = computed(() =>
  corner.value.endsWith("r") ? "flex-row-reverse" : "flex-row",
)
</script>

<template>
  <Transition name="pip-fade">
    <div
      v-if="open"
      class="pip-container fixed z-[9999] flex gap-3 items-end max-w-[calc(100vw-40px)]"
      :class="[posClass, dirClass]"
    >
        <!-- Chat history panel -->
        <Transition name="chat-slide">
          <div
            v-if="showChat"
            class="chat-panel w-[340px] bg-white border-2 border-kiosk-ink rounded-[20px] shadow-[0_24px_48px_rgba(0,0,0,0.18)] flex flex-col overflow-hidden shrink-0"
            :style="{ height: `${SIZES[size].h}px` }"
          >
            <div class="py-3.5 px-4 border-b-2 border-kiosk-line flex justify-between items-center shrink-0">
              <div class="text-base font-bold text-kiosk-ink">對話紀錄</div>
              <button
                class="w-8 h-8 bg-kiosk-soft border-0 rounded-full cursor-pointer text-kiosk-ink inline-flex items-center justify-center p-0"
                @click="showChat = false"
                aria-label="關閉"
              >
                <X class="size-[18px]" :stroke-width="2.6" />
              </button>
            </div>
            <div ref="pip-chat-body" class="flex-1 p-4 flex flex-col gap-2.5 overflow-y-auto">
              <div
                v-for="msg in messages"
                :key="msg.id"
                class="flex"
                :class="msg.role === 'assistant' ? 'justify-start' : 'justify-end'"
              >
                <div
                  class="max-w-[82%] py-2.5 px-3.5 text-sm leading-[1.45] font-medium"
                  :class="msg.role === 'assistant'
                    ? 'bg-kiosk-soft text-kiosk-ink rounded-[16px_16px_16px_4px]'
                    : 'bg-kiosk-ink text-white rounded-[16px_16px_4px_16px]'"
                >
                  {{ msg.text }}
                </div>
              </div>
              <div v-if="isSending" class="flex justify-start">
                <div class="max-w-[82%] py-2.5 px-3.5 bg-kiosk-soft text-kiosk-ink rounded-[16px_16px_16px_4px] text-sm font-medium leading-[1.45] opacity-50 tracking-[0.2em]">…</div>
              </div>
            </div>
            <div class="py-2.5 px-3 border-t-2 border-kiosk-line flex gap-2 items-center shrink-0">
              <input
                v-model="userInput"
                type="text"
                class="flex-1 h-10 border-2 border-kiosk-line rounded-full px-3.5 text-sm font-[inherit] bg-kiosk-soft outline-none focus:border-kiosk-accent"
                placeholder="輸入問題…"
                :disabled="isSending"
                @keydown="handleKeydown"
              />
              <button
                class="w-10 h-10 bg-kiosk-accent border-0 rounded-full text-white cursor-pointer inline-flex items-center justify-center p-0 shrink-0 disabled:bg-kiosk-line disabled:cursor-not-allowed"
                :disabled="isSending || !userInput.trim()"
                @click="sendMessage"
              >
                <Send class="size-[18px]" :stroke-width="2.2" />
              </button>
            </div>
          </div>
        </Transition>

        <!-- PIP frame -->
        <div
          class="pip-frame bg-kiosk-ink rounded-[22px] overflow-hidden relative shadow-[0_24px_48px_rgba(0,0,0,0.20),0_4px_12px_rgba(0,0,0,0.10)] border-[3px] border-white shrink-0"
          :style="{ width: `${SIZES[size].w}px`, height: `${SIZES[size].h}px` }"
        >
          <!-- Top bar -->
          <div class="absolute top-0 left-0 right-0 py-2.5 px-3 bg-kiosk-ink/55 flex justify-between items-center z-[3]">
            <div class="flex items-center gap-2">
              <span class="w-2 h-2 rounded-full bg-kiosk-accent" />
              <span class="text-white text-sm font-bold">小芸</span>
            </div>
            <button
              class="w-8 h-8 bg-white/20 text-white border-0 rounded-full cursor-pointer p-0 inline-flex items-center justify-center"
              @click="emit('close')"
              aria-label="關閉"
            >
              <X class="size-[18px]" :stroke-width="2.6" />
            </button>
          </div>

          <!-- Avatar -->
          <img
            src="/avatar.png"
            alt="虛擬站務員小芸"
            class="absolute inset-0 w-full h-full object-cover object-top"
          />

          <!-- Subtitle -->
          <div class="absolute left-2.5 right-2.5 bottom-16 bg-kiosk-ink/80 py-2.5 px-3 rounded-xl z-[2]">
            <div class="text-white text-[13px] font-semibold leading-[1.45] line-clamp-2">{{ lastAgentText }}</div>
          </div>

          <!-- Move-mode overlay -->
          <div
            v-if="moveMode"
            class="absolute inset-0 bg-kiosk-ink/90 z-[4] pt-[50px] px-4 pb-4 flex flex-col gap-3"
          >
            <div class="text-white text-[13px] font-semibold text-center">請點選要移動到的位置</div>
            <div class="flex-1 grid grid-cols-2 grid-rows-2 gap-2">
              <button
                v-for="c in (['tl','tr','bl','br'] as Corner[])"
                :key="c"
                class="rounded-xl cursor-pointer p-0 flex items-center justify-center"
                :class="c === corner
                  ? 'bg-kiosk-accent border-2 border-kiosk-accent'
                  : 'bg-white/[0.06] border-2 border-dashed border-white/30'"
                @click="corner = c; moveMode = false"
              >
                <span class="w-3 h-3 rounded-full bg-white" />
              </button>
            </div>
            <button
              class="h-10 bg-white/[0.12] border-0 text-white rounded-full text-sm font-bold cursor-pointer font-[inherit]"
              @click="moveMode = false"
            >取消</button>
          </div>

          <!-- Action bar -->
          <div v-else class="absolute left-0 right-0 bottom-0 pt-2 px-2 pb-2.5 flex gap-1.5 z-[2]">
            <button
              class="flex-1 h-11 border-0 rounded-[14px] text-[13px] font-bold cursor-pointer font-[inherit] flex flex-col items-center justify-center gap-0.5 p-0"
              :class="showChat ? 'bg-kiosk-accent text-white' : 'bg-white/[0.92] text-kiosk-ink'"
              @click="toggleChat"
            >
              <MessageSquareText class="size-[18px]" :stroke-width="2.2" />
              <span class="text-xs font-bold leading-none">對話</span>
            </button>
            <button
              class="flex-1 h-11 bg-white/[0.92] text-kiosk-ink border-0 rounded-[14px] text-[13px] font-bold cursor-pointer font-[inherit] flex flex-col items-center justify-center gap-0.5 p-0"
              @click="moveMode = true"
            >
              <Move class="size-[18px]" :stroke-width="2.2" />
              <span class="text-xs font-bold leading-none">移動</span>
            </button>
            <button
              class="flex-1 h-11 bg-white/[0.92] text-kiosk-ink border-0 rounded-[14px] text-[13px] font-bold cursor-pointer font-[inherit] flex flex-col items-center justify-center gap-0.5 p-0"
              @click="cycleSize"
            >
              <Maximize2 class="size-[18px]" :stroke-width="2.2" />
              <span class="text-xs font-bold leading-none">大小</span>
            </button>
          </div>
        </div>
    </div>
  </Transition>
</template>

<style scoped>
/*
 * Vue <Transition> hooks — can't be expressed as Tailwind utility classes because
 * they're generated by name="pip-fade" / "chat-slide" and applied automatically.
 */
.pip-fade-enter-active, .pip-fade-leave-active { transition: opacity 0.2s, transform 0.2s; }
.pip-fade-enter-from, .pip-fade-leave-to { opacity: 0; transform: scale(0.95); }

.chat-slide-enter-active, .chat-slide-leave-active { transition: opacity 0.18s, transform 0.18s; }
.chat-slide-enter-from, .chat-slide-leave-to { opacity: 0; transform: translateX(8px); }

/*
 * Dev-only narrow viewport fallback (kiosk runs at 1280×800 and never triggers this).
 * Uses !important to override inline width/height bindings driving SIZES.
 */
@media (max-width: 760px) {
  .pip-container {
    left: 12px !important;
    right: 12px !important;
    bottom: 12px !important;
    top: auto !important;
    max-width: none;
    flex-direction: column-reverse !important;
    align-items: stretch;
  }
  .pip-frame {
    align-self: flex-end;
    width: min(260px, 58vw) !important;
    height: min(325px, 48vh) !important;
  }
  .chat-panel {
    width: 100%;
    height: min(420px, 52vh) !important;
  }
}
</style>
