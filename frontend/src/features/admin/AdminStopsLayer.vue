<script setup lang="ts">
/**
 * Renders all Yunlin stops as a MapLibre GeoJSON circle layer.
 * One dot per unique stop name. Selected stop is highlighted in amber.
 * Emits `select` on click.
 */
import type { GeoJSONSource, MapMouseEvent } from "maplibre-gl"
import { watch } from "vue"

import { useMap } from "@/components/ui/map"
import { useMapLayer } from "@/components/ui/map/composables/use-map-layer"

import type { StopEntry } from "./api/admin"

const props = defineProps<{
  stops: StopEntry[]
  selectedStopName: string | null
}>()

const emit = defineEmits<{
  select: [stop: StopEntry]
}>()

const { map, isLoaded } = useMap()

const SOURCE = "admin-stops"
const LAYER_ALL = "admin-stops-all"
const LAYER_SELECTED = "admin-stops-selected"

function stopsToGeoJSON(stops: StopEntry[]) {
  return {
    type: "FeatureCollection" as const,
    features: stops.map((s) => ({
      type: "Feature" as const,
      geometry: { type: "Point" as const, coordinates: [s.lng, s.lat] },
      properties: { name: s.name, lat: s.lat, lng: s.lng },
    })),
  }
}

useMapLayer(map, isLoaded, (mapInstance) => {
  mapInstance.addSource(SOURCE, {
    type: "geojson",
    data: stopsToGeoJSON(props.stops),
  })

  // All stops — small blue dots
  mapInstance.addLayer({
    id: LAYER_ALL,
    type: "circle",
    source: SOURCE,
    paint: {
      "circle-radius": 5,
      "circle-color": "#3b82f6",
      "circle-stroke-width": 1.5,
      "circle-stroke-color": "#ffffff",
      "circle-opacity": 0.75,
    },
  })

  // Selected stop — larger amber ring on top
  mapInstance.addLayer({
    id: LAYER_SELECTED,
    type: "circle",
    source: SOURCE,
    filter: props.selectedStopName ? ["==", ["get", "name"], props.selectedStopName] : ["literal", false],
    paint: {
      "circle-radius": 9,
      "circle-color": "#f59e0b",
      "circle-stroke-width": 2,
      "circle-stroke-color": "#ffffff",
      "circle-opacity": 1,
    },
  })

  const enterHandler = () => {
    mapInstance.getCanvas().style.cursor = "pointer"
  }
  const leaveHandler = () => {
    mapInstance.getCanvas().style.cursor = ""
  }
  const clickHandler = (
    e: MapMouseEvent & {
      features?: import("maplibre-gl").MapGeoJSONFeature[]
    },
  ) => {
    const feature = e.features?.[0]
    if (!feature) return
    const p = feature.properties as { name: string; lat: number; lng: number }
    emit("select", { name: p.name, lat: p.lat, lng: p.lng })
  }

  mapInstance.on("mouseenter", LAYER_ALL, enterHandler)
  mapInstance.on("mouseleave", LAYER_ALL, leaveHandler)
  mapInstance.on("click", LAYER_ALL, clickHandler)

  return () => {
    mapInstance.off("mouseenter", LAYER_ALL, enterHandler)
    mapInstance.off("mouseleave", LAYER_ALL, leaveHandler)
    mapInstance.off("click", LAYER_ALL, clickHandler)
    if (mapInstance.getLayer(LAYER_SELECTED)) mapInstance.removeLayer(LAYER_SELECTED)
    if (mapInstance.getLayer(LAYER_ALL)) mapInstance.removeLayer(LAYER_ALL)
    if (mapInstance.getSource(SOURCE)) mapInstance.removeSource(SOURCE)
  }
})

watch(
  () => props.stops,
  (newStops) => {
    const mapInstance = map.value
    if (!mapInstance || !isLoaded.value) return
    const source = mapInstance.getSource(SOURCE) as GeoJSONSource | undefined
    source?.setData(stopsToGeoJSON(newStops))
  },
)

watch(
  () => props.selectedStopName,
  (name) => {
    const mapInstance = map.value
    if (!mapInstance || !isLoaded.value) return
    if (mapInstance.getLayer(LAYER_SELECTED)) {
      mapInstance.setFilter(LAYER_SELECTED, name ? ["==", ["get", "name"], name] : ["literal", false])
    }
  },
)
</script>

<template />
