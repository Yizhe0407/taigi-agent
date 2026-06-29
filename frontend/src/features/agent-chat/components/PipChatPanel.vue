<script setup lang="ts">
import { ref, watch, nextTick, onMounted } from "vue"
import { Send, X } from "@lucide/vue"

import type { PipChatMessage } from "../types"

const props = defineProps<{
  messages: PipChatMessage[]
  isSending: boolean
  heightPx: number
}>()

const userInput = defineModel<string>("userInput", { required: true })

defineEmits<{
  close: []
  send: []
  keydown: [event: KeyboardEvent]
}>()

const inputRef = ref<HTMLInputElement | null>(null)

watch(() => props.isSending, (isSending) => {
  if (!isSending) {
    nextTick(() => {
      inputRef.value?.focus()
    })
  }
})

onMounted(() => {
  inputRef.value?.focus()
})
</script>

<template>
  <div
    class="w-[340px] bg-white border-2 border-kiosk-ink rounded-[20px] shadow-[0_24px_48px_rgba(0,0,0,0.18)] flex flex-col overflow-hidden shrink-0"
    :style="{ height: `${heightPx}px` }"
  >
    <div class="py-3.5 px-4 border-b-2 border-kiosk-line flex justify-between items-center shrink-0">
      <div class="text-base font-bold text-kiosk-ink">對話紀錄</div>
      <button
        class="w-8 h-8 bg-kiosk-soft border-0 rounded-full cursor-pointer text-kiosk-ink inline-flex items-center justify-center p-0"
        aria-label="關閉"
        @click="$emit('close')"
      >
        <X class="size-[18px]" :stroke-width="2.6" />
      </button>
    </div>
    <div class="flex-1 p-4 flex flex-col gap-2.5 overflow-y-auto">
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
        ref="inputRef"
        v-model="userInput"
        type="text"
        class="flex-1 min-w-0 h-10 border-2 border-kiosk-line rounded-full px-3.5 text-sm font-[inherit] bg-kiosk-soft outline-none focus:border-kiosk-accent"
        placeholder="輸入問題…"
        :disabled="isSending"
        @keydown="$emit('keydown', $event)"
      />
      <button
        class="w-10 h-10 bg-kiosk-accent border-0 rounded-full text-white cursor-pointer inline-flex items-center justify-center p-0 shrink-0 disabled:bg-kiosk-line disabled:cursor-not-allowed"
        :disabled="isSending || !userInput.trim()"
        @click="$emit('send')"
      >
        <Send class="size-[18px]" :stroke-width="2.2" />
      </button>
    </div>
  </div>
</template>
