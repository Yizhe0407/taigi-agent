<script setup lang="ts">
defineProps<{ secondsLeft: number }>()

defineEmits<{
  dismiss: []
}>()
</script>

<template>
  <div class="absolute inset-0 bg-kiosk-ink/92 z-[4] flex flex-col items-center justify-center gap-3 px-5 text-center backdrop-blur-sm">
    <div class="text-white text-[15px] font-bold leading-[1.5]">還在嗎？</div>
    <div class="text-white/55 text-[12px] font-medium">{{ secondsLeft }} 秒後自動關閉</div>
    <button
      class="h-10 px-6 bg-kiosk-accent text-white border-0 rounded-[12px] text-[13px] font-bold cursor-pointer font-[inherit]"
      @click="$emit('dismiss')"
    >
      繼續對話
    </button>
  </div>
</template>
