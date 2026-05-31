<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from "vue"
import { OfficialCubismAvatar } from "../live2d/officialCubismAvatar"

const props = defineProps<{
  modelSrc: string
  fallbackSrc: string
  mouthAmplitude: number
}>()

const host = ref<HTMLDivElement | null>(null)
const isReady = ref(false)

let avatar: OfficialCubismAvatar | null = null
let resizeObserver: ResizeObserver | null = null

watch(
  () => props.mouthAmplitude,
  (v) => avatar?.setMouthAmplitude(v),
)

onMounted(async () => {
  if (!host.value) return

  try {
    avatar = new OfficialCubismAvatar(host.value, props.modelSrc)
    await avatar.load()

    resizeObserver = new ResizeObserver(() => avatar?.resize())
    resizeObserver.observe(host.value)
    isReady.value = true
  }
  catch (error) {
    console.error("Live2D avatar failed to load", error)
    destroyLive2D()
  }
})

onBeforeUnmount(() => {
  destroyLive2D()
})

function destroyLive2D() {
  resizeObserver?.disconnect()
  resizeObserver = null
  avatar?.dispose()
  avatar = null

  isReady.value = false
}
</script>

<template>
  <div ref="host" class="absolute inset-0 bg-kiosk-ink">
    <img
      v-if="!isReady"
      :src="fallbackSrc"
      alt="虛擬站務員小芸"
      class="absolute inset-0 h-full w-full object-cover object-top"
    />
  </div>
</template>
