<script setup lang="ts">
import type { GeoJSONSource } from "maplibre-gl"
import { useId, watch } from "vue"

import { useMap } from "./composables/use-map"
import { useMapLayer } from "./composables/use-map-layer"

type Props = {
  id?: string
  coordinates: [number, number][]
  color?: string
  width?: number
  opacity?: number
  dashArray?: [number, number]
  /** "round" (default) looks good for solid bus lines; "butt" needed for visible dashes */
  lineCap?: "round" | "butt" | "square"
  lineJoin?: "round" | "bevel" | "miter"
  interactive?: boolean
}

const props = withDefaults(defineProps<Props>(), {
  color: "#2563eb",
  width: 4,
  opacity: 0.85,
  lineCap: "round",
  lineJoin: "round",
  interactive: true,
})
const emit = defineEmits<{
  click: []
}>()
const { map, isLoaded } = useMap()
const autoId = useId()
const baseId = props.id ?? autoId
const sourceId = `route-source-${baseId}`
const layerId = `route-layer-${baseId}`

const buildFeature = (coordinates: [number, number][]) =>
  ({
    type: "Feature",
    properties: {},
    geometry: { type: "LineString", coordinates },
  }) as const

useMapLayer(map, isLoaded, (mapInstance) => {
  mapInstance.addSource(sourceId, {
    type: "geojson",
    data: buildFeature(props.coordinates),
  })
  mapInstance.addLayer({
    id: layerId,
    type: "line",
    source: sourceId,
    layout: {
      "line-join": props.lineJoin,
      "line-cap": props.lineCap,
    },
    paint: {
      "line-color": props.color,
      "line-width": props.width,
      "line-opacity": props.opacity,
      ...(props.dashArray && { "line-dasharray": props.dashArray }),
    },
  })

  const onClick = () => emit("click")
  if (props.interactive) mapInstance.on("click", layerId, onClick)

  return () => {
    if (props.interactive) mapInstance.off("click", layerId, onClick)
    try {
      if (mapInstance.getLayer(layerId)) mapInstance.removeLayer(layerId)
      if (mapInstance.getSource(sourceId)) mapInstance.removeSource(sourceId)
    } catch {
      // MapLibre removes custom layers when a basemap style changes.
    }
  }
})

watch(
  () => props.coordinates,
  (coordinates) => {
    if (coordinates.length < 2) return
    const source = map.value?.getSource(sourceId) as GeoJSONSource | undefined
    source?.setData(buildFeature(coordinates))
  },
  { deep: true },
)
</script>

<template></template>
