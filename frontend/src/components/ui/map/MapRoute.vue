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

const lifecycle = useMapLayer(
  map,
  isLoaded,
  `map route ${baseId}`,
  (mapInstance, owner, fail) => {
    const onClick = () => {
      if (owner.signal.aborted) return
      try {
        emit("click")
      } catch (error) {
        fail(error)
      }
    }
    const interactive = props.interactive

    owner.acquire(
      () =>
        mapInstance.addSource(sourceId, {
          type: "geojson",
          data: buildFeature(props.coordinates),
        }),
      () => {
        if (mapInstance.getSource(sourceId)) mapInstance.removeSource(sourceId)
      },
    )

    owner.acquire(
      () =>
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
        }),
      () => {
        if (mapInstance.getLayer(layerId)) mapInstance.removeLayer(layerId)
      },
    )

    if (interactive) {
      owner.acquire(
        () => mapInstance.on("click", layerId, onClick),
        () => mapInstance.off("click", layerId, onClick),
      )
    }
  },
)

watch(
  () => props.coordinates,
  (coordinates) => {
    if (coordinates.length < 2) return
    const mapInstance = map.value
    if (!mapInstance || !isLoaded.value || !lifecycle.isActive(mapInstance)) {
      return
    }

    try {
      const source = mapInstance.getSource(sourceId) as GeoJSONSource | undefined
      source?.setData(buildFeature(coordinates))
    } catch (error) {
      lifecycle.fail(error, mapInstance)
    }
  },
  { deep: true },
)
</script>

<template></template>
