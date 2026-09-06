<script setup lang="ts">
import { onMounted, ref, watch } from "vue"

import { observeAsynchronousScopeTeardown } from "@/lib/component-lifecycle"
import {
  createResourceOwner,
  type ResourceClaim,
  type ResourceReleaseAttempt,
} from "@/lib/resource-owner"

import type { ExpressionState } from "../live2d/expressionStates"
import { OfficialCubismAvatar } from "../live2d/officialCubismAvatar"

const props = defineProps<{
  modelSrc: string
  fallbackSrc: string
  mouthAmplitude: number
  expressionState: ExpressionState
}>()

const host = ref<HTMLDivElement | null>(null)
const isReady = ref(false)
const resources = createResourceOwner("Live2D avatar component")

let avatar: OfficialCubismAvatar | null = null
let loadTask: Promise<void> | null = null
let loadAbortClaim: ResourceClaim | null = null

function throwFailures(failures: unknown[], message: string): void {
  if (failures.length === 1) throw failures[0]
  if (failures.length > 1) throw new AggregateError(failures, message)
}

function releaseResources(attempt: ResourceReleaseAttempt): void {
  resources.dispose(attempt)
  if (resources.settled) avatar = null
}

function failLive2D(
  error: unknown,
  attempt: ResourceReleaseAttempt = {},
): never {
  isReady.value = false
  try {
    releaseResources(attempt)
  } catch (cleanupError) {
    throw new AggregateError(
      [error, cleanupError],
      "Live2D failed and could not be fully released",
    )
  }
  throw error
}

watch(
  () => props.mouthAmplitude,
  (value) => {
    if (resources.disposed || !avatar) return
    try {
      avatar.setMouthAmplitude(value)
    } catch (error) {
      failLive2D(error)
    }
  },
)

// No { immediate: true } — the avatar instance doesn't exist until onMounted
// creates it, so an immediate first call would just no-op anyway. load()
// applies the initial "idle" pose itself once the model is ready.
watch(
  () => props.expressionState,
  (value) => {
    if (resources.disposed || !avatar) return
    try {
      avatar.setExpressionState(value)
    } catch (error) {
      failLive2D(error)
    }
  },
)

async function loadLive2D(
  current: OfficialCubismAvatar,
  controller: AbortController,
  setupAttempt: ResourceReleaseAttempt,
): Promise<void> {
  try {
    await current.load(controller.signal)
    if (resources.disposed || controller.signal.aborted || avatar !== current || !host.value) return

    let observer: ResizeObserver | null = null
    const observerClaim = resources.claim("Live2D resize observer", () => {
      observer?.disconnect()
    })
    try {
      observer = new ResizeObserver(() => {
        if (resources.disposed || avatar !== current) return
        try {
          current.resize()
        } catch (error) {
          failLive2D(error)
        }
      })
      observer.observe(host.value)
    } catch (setupError) {
      try {
        observerClaim.release(setupAttempt)
      } catch (cleanupError) {
        throw new AggregateError(
          [setupError, cleanupError],
          "Failed to register and roll back Live2D resize observer",
        )
      }
      throw setupError
    }

    if (resources.disposed || avatar !== current) return
    isReady.value = true
  } catch (error) {
    const aborted = error instanceof DOMException && error.name === "AbortError"
    if (resources.disposed) {
      if (aborted) return
      throw error
    }
    if (avatar !== current) return
    isReady.value = false
    try {
      releaseResources(setupAttempt)
    } catch (cleanupError) {
      throw new AggregateError(
        [error, cleanupError],
        "Failed to load and roll back Live2D avatar",
      )
    }
    if (!aborted) throw error
  } finally {
    if (loadAbortClaim?.active && !resources.disposed) loadAbortClaim.transfer()
  }
}

onMounted(() => {
  const target = host.value
  if (resources.disposed || !target) return
  const attempt: ResourceReleaseAttempt = {}

  try {
    const controllerAcquisition = resources.acquire(
      () => new AbortController(),
      controller => controller?.abort(),
      "Live2D load abort controller",
      attempt,
    )
    loadAbortClaim = controllerAcquisition
    const avatarAcquisition = resources.acquire(
      () => new OfficialCubismAvatar(target, props.modelSrc, failLive2D),
      (current, releaseAttempt) => current?.dispose(releaseAttempt),
      "Live2D avatar",
      attempt,
    )
    avatar = avatarAcquisition.value

    let operation!: Promise<void>
    operation = loadLive2D(
      avatar,
      controllerAcquisition.value,
      attempt,
    ).finally(() => {
      if (loadTask === operation) loadTask = null
    })
    loadTask = operation
    return operation
  } catch (setupError) {
    isReady.value = false
    try {
      releaseResources(attempt)
    } catch (cleanupError) {
      throw new AggregateError(
        [setupError, cleanupError],
        "Failed to initialize and roll back Live2D avatar",
      )
    }
    throw setupError
  }
})

const settleLive2DTeardown = observeAsynchronousScopeTeardown(
  "Live2D avatar teardown",
  async (attempt) => {
    isReady.value = false
    const failures: unknown[] = []
    try {
      releaseResources(attempt)
    } catch (error) {
      failures.push(error)
    }
    const task = loadTask
    if (task) {
      try {
        await task
      } catch (error) {
        failures.push(error)
      }
    }
    throwFailures(failures, "Failed to release Live2D component resources")
  },
)

defineExpose({ settleLive2DTeardown })
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
