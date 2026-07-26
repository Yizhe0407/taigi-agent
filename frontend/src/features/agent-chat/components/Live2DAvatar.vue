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
let loadAbort: AbortController | null = null

watch(
  () => props.mouthAmplitude,
  (v) => avatar?.setMouthAmplitude(v),
)

onMounted(async () => {
  const target = host.value
  if (!target) return

  const current = new OfficialCubismAvatar(target, props.modelSrc)
  const controller = new AbortController()
  avatar = current
  loadAbort = controller
  try {
    await current.load(controller.signal)
    if (controller.signal.aborted || avatar !== current || !host.value) {
      current.dispose()
      return
    }

    resizeObserver = new ResizeObserver(() => current.resize())
    resizeObserver.observe(host.value)
    isReady.value = true
  }
  catch (error) {
    if (!(error instanceof DOMException && error.name === "AbortError")) {
      console.error("Live2D avatar failed to load", error)
    }
    current.dispose()
    if (avatar === current) avatar = null
  }
  finally {
    if (loadAbort === controller) loadAbort = null
  }
})

onBeforeUnmount(() => {
  destroyLive2D()
})

function destroyLive2D() {
  loadAbort?.abort()
  loadAbort = null
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
