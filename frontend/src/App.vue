<script setup lang="ts">
import { BusFront, Map, MessageSquare } from "@lucide/vue"
import { ref } from "vue"

import AgentChatView from "@/features/agent-chat/components/AgentChatView.vue"
import RoutePlannerView from "@/features/route-planner/components/RoutePlannerView.vue"

type Tab = "chat" | "route"

const activeTab = ref<Tab>("chat")
</script>

<template>
  <div class="flex h-svh min-h-[40rem] flex-col overflow-hidden bg-background">
    <!-- Shared header -->
    <header
      class="z-20 flex h-16 shrink-0 items-center justify-between gap-4 border-b border-border bg-background/95 px-4 backdrop-blur sm:px-6"
    >
      <!-- Brand -->
      <div class="flex min-w-0 items-center gap-3">
        <div
          class="grid size-10 shrink-0 place-items-center rounded-md bg-primary text-primary-foreground"
        >
          <BusFront class="size-5" />
        </div>
        <div class="min-w-0 hidden sm:block">
          <p class="truncate text-sm font-semibold text-foreground">
            雲林公車助理
          </p>
          <p class="truncate text-xs text-muted-foreground">雲林科技大學</p>
        </div>
      </div>

      <!-- Tab navigation -->
      <nav class="flex items-center gap-1">
        <button
          type="button"
          class="flex items-center gap-2 rounded-lg px-4 py-2 text-sm font-medium transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          :class="
            activeTab === 'chat'
              ? 'bg-primary text-primary-foreground'
              : 'text-muted-foreground hover:bg-accent hover:text-foreground'
          "
          @click="activeTab = 'chat'"
        >
          <MessageSquare class="size-4" />
          對話助理
        </button>
        <button
          type="button"
          class="flex items-center gap-2 rounded-lg px-4 py-2 text-sm font-medium transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          :class="
            activeTab === 'route'
              ? 'bg-primary text-primary-foreground'
              : 'text-muted-foreground hover:bg-accent hover:text-foreground'
          "
          @click="activeTab = 'route'"
        >
          <Map class="size-4" />
          路線規劃
        </button>
      </nav>
    </header>

    <!-- Content area — both views stay mounted (v-show) to preserve state -->
    <main class="min-h-0 flex-1">
      <AgentChatView v-show="activeTab === 'chat'" class="h-full" />
      <RoutePlannerView v-show="activeTab === 'route'" class="h-full" />
    </main>
  </div>
</template>
