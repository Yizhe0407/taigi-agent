<script setup lang="ts">
import { RouterView } from "vue-router"

import PipAgentOverlay from "@/features/agent-chat/components/PipAgentOverlay.vue"
import { usePip } from "@/features/agent-chat/composables/usePip"

const { isOpen, close } = usePip()
</script>

<template>
  <div class="relative w-screen h-screen font-tc antialiased overflow-hidden">
    <RouterView v-slot="{ Component }">
      <component :is="Component" class="w-full h-full" />
    </RouterView>
    <PipAgentOverlay :open="isOpen" @close="close" />
  </div>
</template>
