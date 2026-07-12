<script setup lang="ts">
defineProps<{ secondsLeft: number }>()

defineEmits<{
  end: []
  continue: []
}>()
</script>

<template>
  <div class="absolute inset-0 bg-kiosk-ink/92 z-[4] flex flex-col items-center justify-center gap-3 px-5 text-center backdrop-blur-sm">
    <div class="text-white text-[15px] font-bold leading-[1.5]">對話結束囉，猶有問題無？</div>
    <div class="text-white/55 text-[12px] font-medium">{{ secondsLeft }} 秒後自動結束</div>
    <div class="flex gap-2 w-full mt-1">
      <button
        class="flex-1 h-10 bg-white/10 text-white border border-white/20 rounded-[12px] text-[13px] font-bold cursor-pointer font-[inherit]"
        @click="$emit('continue')"
      >
        閣繼續問
      </button>
      <button
        class="flex-1 h-10 bg-kiosk-accent text-white border-0 rounded-[12px] text-[13px] font-bold cursor-pointer font-[inherit]"
        @click="$emit('end')"
      >
        結束
      </button>
    </div>
  </div>
</template>
