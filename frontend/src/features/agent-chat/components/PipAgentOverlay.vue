<script setup lang="ts">
import { computed, ref, toRef, watch } from "vue"

import { usePipChat } from "@/features/agent-chat/composables/usePipChat"

import PipChatPanel from "./PipChatPanel.vue"
import PipFrame from "./PipFrame.vue"
import { PIP_SIZES, type PipCorner, type PipSize } from "../types"

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ close: [] }>()

const corner = ref<PipCorner>("br")
const size = ref<PipSize>("lg")
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

const frameSize = computed(() => PIP_SIZES[size.value])

function cycleSize() {
  size.value = size.value === "sm" ? "md" : size.value === "md" ? "lg" : "sm"
}

function selectCorner(next: PipCorner) {
  corner.value = next
  moveMode.value = false
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
  <Transition
    enter-active-class="transition-[opacity,transform] duration-200"
    enter-from-class="opacity-0 scale-95"
    leave-active-class="transition-[opacity,transform] duration-200"
    leave-to-class="opacity-0 scale-95"
  >
    <div
      v-if="open"
      class="fixed z-[9999] flex gap-3 items-end max-w-[calc(100vw-40px)]"
      :class="[posClass, dirClass]"
    >
      <Transition
        enter-active-class="transition-[opacity,transform] duration-[180ms]"
        enter-from-class="opacity-0 translate-x-2"
        leave-active-class="transition-[opacity,transform] duration-[180ms]"
        leave-to-class="opacity-0 translate-x-2"
      >
        <PipChatPanel
          v-if="showChat"
          v-model:user-input="userInput"
          :messages="messages"
          :is-sending="isSending"
          :height-px="frameSize.h"
          @close="showChat = false"
          @send="sendMessage"
          @keydown="handleKeydown"
        />
      </Transition>

      <PipFrame
        :width="frameSize.w"
        :height="frameSize.h"
        :last-agent-text="lastAgentText"
        :show-chat="showChat"
        :move-mode="moveMode"
        :corner="corner"
        @close="emit('close')"
        @toggle-chat="toggleChat"
        @enter-move="moveMode = true"
        @cycle-size="cycleSize"
        @select-corner="selectCorner"
        @cancel-move="moveMode = false"
      />
    </div>
  </Transition>
</template>
