<script setup lang="ts">
import "maplibre-gl/dist/maplibre-gl.css"

import type {
  MapOptions,
  ProjectionSpecification,
  StyleSpecification,
} from "maplibre-gl"
import MapLibreGL from "maplibre-gl"
import {
  computed,
  onMounted,
  provide,
  ref,
  shallowRef,
  useAttrs,
  useTemplateRef,
  watch,
} from "vue"

import {
  currentSynchronousScopeReleaseAttempt,
  observeSynchronousScopeTeardown,
} from "@/lib/component-lifecycle"
import {
  createResourceOwner,
  type ResourceReleaseAttempt,
} from "@/lib/resource-owner"
import { createOwnedTimeout, type TimerLease } from "@/lib/timer-owner"
import { cn } from "@/lib/utils"
import { useResolvedTheme } from "./composables/use-resolved-theme"
import { MapContextKey } from "./context"
import type { MapViewport, Theme } from "./types"
import { getViewport } from "./utils"

type MapStyleOption = string | StyleSpecification

type MapStyles = {
  light?: MapStyleOption
  dark?: MapStyleOption
}

type Props = {
  class?: string
  theme?: Theme
  styles?: MapStyles
  /** When set, overrides both light and dark styles with this URL/spec. */
  styleOverride?: MapStyleOption
  projection?: ProjectionSpecification
  viewport?: Partial<MapViewport>
  loading?: boolean
}

defineOptions({ inheritAttrs: false })

const props = withDefaults(defineProps<Props>(), {
  loading: false,
})
const attrs = useAttrs()
const emit = defineEmits<{
  "update:viewport": [viewport: MapViewport]
}>()

const defaultStyles = {
  dark: "https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json",
  light: "https://basemaps.cartocdn.com/gl/positron-gl-style/style.json",
}

const containerRef = useTemplateRef<HTMLDivElement>("container")
const mapInstance = shallowRef<MapLibreGL.Map | null>(null)
const isLoaded = ref(false)
const isStyleLoaded = ref(false)
const mapOwner = createResourceOwner("MapLibre map")
const resolvedTheme = useResolvedTheme(mapOwner, () => props.theme)
const isControlled = computed(() => props.viewport !== undefined)
const mapStyles = computed<Required<MapStyles>>(() => ({
  dark: props.styleOverride ?? props.styles?.dark ?? defaultStyles.dark,
  light: props.styleOverride ?? props.styles?.light ?? defaultStyles.light,
}))
const containerClass = computed(() => cn("relative h-full w-full", props.class))

let ownedMap: MapLibreGL.Map | null = null
let currentStyle: MapStyleOption | null = null
let styleTimer: TimerLease | null = null
let internalUpdate = false

const cancelStyleTimer = (attempt?: ResourceReleaseAttempt) => {
  const timer = styleTimer
  if (!timer) return
  timer.cancel(attempt)
  if (styleTimer === timer) styleTimer = null
}

const isLoadedAndStyleLoaded = computed(
  () => isLoaded.value && isStyleLoaded.value,
)

provide(MapContextKey, {
  map: mapInstance,
  isLoaded: isLoadedAndStyleLoaded,
})

const reservedAttrs = new Set(["class", "style", "container"])

const collectMapOptions = (): Partial<MapOptions> => {
  const options: Record<string, unknown> = {}
  for (const [key, value] of Object.entries(attrs)) {
    if (value === undefined) continue
    if (/^on[A-Z]/.test(key)) continue
    if (reservedAttrs.has(key)) continue
    options[key] = value
  }
  for (const [key, value] of Object.entries(props.viewport ?? {})) {
    if (value !== undefined) options[key] = value
  }
  return options as Partial<MapOptions>
}

function cleanupMap(
  attempt: ResourceReleaseAttempt,
  unresolvedFailureAlreadyRepresented = false,
): boolean {
  const failures: unknown[] = []

  // Publish the permanent callback/acquisition gate before reactive state
  // changes can synchronously unmount descendants or re-enter MapLibre.
  let closeFailed = false
  try {
    mapOwner.close(undefined, attempt)
  } catch (failure) {
    closeFailed = true
    failures.push(failure)
  }

  for (const clearState of [
    () => { mapInstance.value = null },
    () => { isLoaded.value = false },
    () => { isStyleLoaded.value = false },
  ]) {
    try {
      clearState()
    } catch (failure) {
      failures.push(failure)
    }
  }
  currentStyle = null
  internalUpdate = false

  let disposeFailed = false
  try {
    mapOwner.dispose(attempt)
  } catch (failure) {
    disposeFailed = true
    failures.push(failure)
  } finally {
    if (styleTimer && !styleTimer.active) styleTimer = null
  }

  if (mapOwner.settled) {
    ownedMap = null
    styleTimer = null
  } else if (
    !unresolvedFailureAlreadyRepresented
    && !closeFailed
    && !disposeFailed
  ) {
    failures.push(new Error("MapLibre map cleanup remains unresolved"))
  }

  if (failures.length === 1) throw failures[0]
  if (failures.length > 1) {
    throw new AggregateError(failures, "Failed to release MapLibre map")
  }
  return mapOwner.settled
}

function disposeMap(attempt: ResourceReleaseAttempt) {
  cleanupMap(attempt)
}

function failMap(error: unknown, attempt: ResourceReleaseAttempt): never {
  try {
    cleanupMap(attempt, true)
  } catch (cleanupError) {
    throw new AggregateError(
      [error, cleanupError],
      "Map failed and could not be fully released",
    )
  }
  throw error
}

function applyChineseLabels(map: MapLibreGL.Map) {
  map.getStyle().layers.forEach((layer) => {
    if (layer.type !== "symbol") return
    const field = (layer.layout as Record<string, unknown>)?.["text-field"]
    if (!field) return
    map.setLayoutProperty(layer.id, "text-field", [
      "coalesce",
      ["get", "name:zh-Hant"],
      ["get", "name:zh"],
      ["get", "name"],
    ])
  })
}

onMounted(() => {
  if (mapOwner.disposed || !containerRef.value) return
  const setupAttempt = currentSynchronousScopeReleaseAttempt() ?? {}

  const initialStyle =
    resolvedTheme.value === "dark" ? mapStyles.value.dark : mapStyles.value.light
  currentStyle = initialStyle

  try {
    const acquiredMap = mapOwner.acquire(
      () => new MapLibreGL.Map({
        container: containerRef.value!,
        style: initialStyle,
        renderWorldCopies: false,
        attributionControl: { compact: true },
        ...collectMapOptions(),
      }),
      (map) => map?.remove(),
      "MapLibre map instance",
      setupAttempt,
    ).value
    ownedMap = acquiredMap

    const loadHandler = () => {
      if (mapOwner.disposed || ownedMap !== acquiredMap) return
      const attempt: ResourceReleaseAttempt = {}
      try {
        isLoaded.value = true
        const attribution = acquiredMap
          .getContainer()
          .querySelector<HTMLElement>(".maplibregl-ctrl-attrib")
        attribution?.classList.remove("maplibregl-compact-show")
      } catch (error) {
        if (mapOwner.disposed) throw error
        failMap(error, attempt)
      }
    }

    const styleDataHandler = () => {
      if (mapOwner.disposed || ownedMap !== acquiredMap) return
      const attempt: ResourceReleaseAttempt = {}
      try {
        cancelStyleTimer(attempt)
        let timer!: TimerLease
        timer = createOwnedTimeout(mapOwner, "MapLibre style stabilization", (timerAttempt) => {
          if (styleTimer === timer) styleTimer = null
          if (mapOwner.disposed || ownedMap !== acquiredMap) return

          try {
            if (props.projection) acquiredMap.setProjection(props.projection)
            applyChineseLabels(acquiredMap)
            if (
              !mapOwner.disposed && ownedMap === acquiredMap
            ) {
              isStyleLoaded.value = true
            }
          } catch (error) {
            if (mapOwner.disposed) throw error
            failMap(error, timerAttempt)
          }
        }, 100, attempt)
        if (timer.active && !mapOwner.disposed) styleTimer = timer
      } catch (error) {
        if (mapOwner.disposed) throw error
        failMap(error, attempt)
      }
    }

    const moveHandler = () => {
      if (
        mapOwner.disposed ||
        ownedMap !== acquiredMap ||
        internalUpdate
      ) {
        return
      }
      const attempt: ResourceReleaseAttempt = {}
      try {
        emit("update:viewport", getViewport(acquiredMap))
      } catch (error) {
        if (mapOwner.disposed) throw error
        failMap(error, attempt)
      }
    }

    mapOwner.acquire(
      () => acquiredMap.on("load", loadHandler),
      () => acquiredMap.off("load", loadHandler),
      "MapLibre load listener",
      setupAttempt,
    )
    mapOwner.acquire(
      () => acquiredMap.on("styledata", styleDataHandler),
      () => acquiredMap.off("styledata", styleDataHandler),
      "MapLibre style listener",
      setupAttempt,
    )
    mapOwner.acquire(
      () => acquiredMap.on("move", moveHandler),
      () => acquiredMap.off("move", moveHandler),
      "MapLibre move listener",
      setupAttempt,
    )

    mapInstance.value = acquiredMap
  } catch (error) {
    if (mapOwner.disposed) throw error
    failMap(error, setupAttempt)
  }
})

const settleMapTeardown = observeSynchronousScopeTeardown(
  "Map teardown",
  disposeMap,
)

defineExpose({ map: mapInstance, settleMapTeardown })

watch(
  () => props.viewport,
  (next) => {
    const map = mapInstance.value
    if (
      mapOwner.disposed ||
      !map ||
      map !== ownedMap ||
      !isControlled.value ||
      !next ||
      map.isMoving()
    ) {
      return
    }
    const attempt: ResourceReleaseAttempt = {}

    try {
      const current = getViewport(map)
      const target = {
        center: next.center ?? current.center,
        zoom: next.zoom ?? current.zoom,
        bearing: next.bearing ?? current.bearing,
        pitch: next.pitch ?? current.pitch,
      }
      if (
        target.center[0] === current.center[0] &&
        target.center[1] === current.center[1] &&
        target.zoom === current.zoom &&
        target.bearing === current.bearing &&
        target.pitch === current.pitch
      ) {
        return
      }

      internalUpdate = true
      map.jumpTo(target)
    } catch (error) {
      if (mapOwner.disposed) throw error
      failMap(error, attempt)
    } finally {
      internalUpdate = false
    }
  },
  { deep: true },
)

watch([resolvedTheme, mapStyles], ([theme, styles]) => {
  const map = mapInstance.value
  if (mapOwner.disposed || !map || map !== ownedMap) return

  const nextStyle = theme === "dark" ? styles.dark : styles.light
  if (currentStyle === nextStyle) return
  const attempt: ResourceReleaseAttempt = {}

  try {
    cancelStyleTimer(attempt)
    currentStyle = nextStyle
    isStyleLoaded.value = false
    map.setStyle(nextStyle, { diff: true })
  } catch (error) {
    if (mapOwner.disposed) throw error
    failMap(error, attempt)
  }
})

watch(
  () => props.projection,
  (next) => {
    const map = mapInstance.value
    if (mapOwner.disposed || !next || !map || map !== ownedMap) return
    const attempt: ResourceReleaseAttempt = {}
    try {
      map.setProjection(next)
    } catch (error) {
      if (mapOwner.disposed) throw error
      failMap(error, attempt)
    }
  },
)
</script>

<template>
  <div ref="container" :class="containerClass">
    <div
      v-if="!isLoaded || loading"
      class="absolute inset-0 z-10 flex items-center justify-center bg-background/50 backdrop-blur-xs"
    >
      <div class="flex gap-1">
        <span class="size-1.5 animate-pulse rounded-full bg-muted-foreground/60" />
        <span
          class="size-1.5 animate-pulse rounded-full bg-muted-foreground/60 [animation-delay:150ms]"
        />
        <span
          class="size-1.5 animate-pulse rounded-full bg-muted-foreground/60 [animation-delay:300ms]"
        />
      </div>
    </div>
    <slot v-if="mapInstance" />
  </div>
</template>
