<script setup lang="ts">
import type { MarkerOptions } from "maplibre-gl"
import MapLibreGL from "maplibre-gl"
import {
  onBeforeUnmount,
  onMounted,
  provide,
  shallowRef,
  useAttrs,
  watch,
  watchEffect,
} from "vue"

import { useMap } from "./composables/use-map"
import { MarkerContextKey } from "./context"

type Props = {
  longitude: number
  latitude: number
  draggable?: boolean
  offset?: MarkerOptions["offset"]
  rotation?: number
  rotationAlignment?: MarkerOptions["rotationAlignment"]
  pitchAlignment?: MarkerOptions["pitchAlignment"]
}

defineOptions({ inheritAttrs: false })

const props = withDefaults(defineProps<Props>(), {
  draggable: false,
})
const attrs = useAttrs()
const emit = defineEmits<{
  click: [event: MouseEvent]
  drag: [lngLat: { lng: number; lat: number }]
}>()
const { map } = useMap()
const markerRef = shallowRef<MapLibreGL.Marker | null>(null)

provide(MarkerContextKey, { marker: markerRef, map })

const collectMarkerOptions = (): Partial<MarkerOptions> => {
  const options: Record<string, unknown> = {}
  for (const [key, value] of Object.entries(attrs)) {
    if (value === undefined) continue
    if (/^on[A-Z]/.test(key)) continue
    if (key === "class" || key === "style" || key === "element") continue
    options[key] = value
  }
  return options as Partial<MarkerOptions>
}

onMounted(() => {
  const marker = new MapLibreGL.Marker({
    ...collectMarkerOptions(),
    element: document.createElement("div"),
    draggable: props.draggable,
    offset: props.offset,
    rotation: props.rotation,
    rotationAlignment: props.rotationAlignment,
    pitchAlignment: props.pitchAlignment,
  }).setLngLat([props.longitude, props.latitude])

  const element = marker.getElement()
  const onClick = (event: MouseEvent) => emit("click", event)
  element.addEventListener("click", onClick)
  marker.on("drag", () => {
    const { lng, lat } = marker.getLngLat()
    emit("drag", { lng, lat })
  })

  if (map.value) marker.addTo(map.value)
  markerRef.value = marker

  onBeforeUnmount(() => {
    element.removeEventListener("click", onClick)
    marker.remove()
    markerRef.value = null
  })
})

watch(map, (nextMap) => {
  if (nextMap && markerRef.value) markerRef.value.addTo(nextMap)
})

watchEffect(() => {
  const marker = markerRef.value
  if (!marker) return
  marker.setLngLat([props.longitude, props.latitude])
  marker.setDraggable(props.draggable)
  marker.setOffset(props.offset ?? [0, 0])
  marker.setRotation(props.rotation ?? 0)
  marker.setRotationAlignment(props.rotationAlignment ?? "auto")
  marker.setPitchAlignment(props.pitchAlignment ?? "auto")
})
</script>

<template>
  <slot />
</template>
