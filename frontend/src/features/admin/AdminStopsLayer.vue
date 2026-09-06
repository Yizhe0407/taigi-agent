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

const lifecycle = useMapLayer(
  map,
  isLoaded,
  "admin stops map layer",
  (mapInstance, owner, fail) => {
    owner.acquire(
      () =>
        mapInstance.addSource(SOURCE, {
          type: "geojson",
          data: stopsToGeoJSON(props.stops),
        }),
      () => {
        if (mapInstance.getSource(SOURCE)) mapInstance.removeSource(SOURCE)
      },
    )

    owner.acquire(
      () =>
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
        }),
      () => {
        if (mapInstance.getLayer(LAYER_ALL)) mapInstance.removeLayer(LAYER_ALL)
      },
    )

    owner.acquire(
      () =>
        mapInstance.addLayer({
          id: LAYER_SELECTED,
          type: "circle",
          source: SOURCE,
          filter: props.selectedStopName
            ? ["==", ["get", "name"], props.selectedStopName]
            : ["literal", false],
          paint: {
            "circle-radius": 9,
            "circle-color": "#f59e0b",
            "circle-stroke-width": 2,
            "circle-stroke-color": "#ffffff",
            "circle-opacity": 1,
          },
        }),
      () => {
        if (mapInstance.getLayer(LAYER_SELECTED)) {
          mapInstance.removeLayer(LAYER_SELECTED)
        }
      },
    )

    const canvas = mapInstance.getCanvas()
    owner.acquire(
      () => canvas,
      () => {
        canvas.style.cursor = ""
      },
    )

    const enterHandler = () => {
      if (owner.signal.aborted) return
      try {
        canvas.style.cursor = "pointer"
      } catch (error) {
        fail(error)
      }
    }
    const leaveHandler = () => {
      if (owner.signal.aborted) return
      try {
        canvas.style.cursor = ""
      } catch (error) {
        fail(error)
      }
    }
    const clickHandler = (
      event: MapMouseEvent & {
        features?: import("maplibre-gl").MapGeoJSONFeature[]
      },
    ) => {
      if (owner.signal.aborted) return
      try {
        const feature = event.features?.[0]
        if (!feature) return
        const properties = feature.properties as {
          name: string
          lat: number
          lng: number
        }
        emit("select", properties)
      } catch (error) {
        fail(error)
      }
    }

    owner.acquire(
      () => mapInstance.on("mouseenter", LAYER_ALL, enterHandler),
      () => mapInstance.off("mouseenter", LAYER_ALL, enterHandler),
    )
    owner.acquire(
      () => mapInstance.on("mouseleave", LAYER_ALL, leaveHandler),
      () => mapInstance.off("mouseleave", LAYER_ALL, leaveHandler),
    )
    owner.acquire(
      () => mapInstance.on("click", LAYER_ALL, clickHandler),
      () => mapInstance.off("click", LAYER_ALL, clickHandler),
    )
  },
)

watch(
  () => props.stops,
  (newStops) => {
    const mapInstance = map.value
    if (!mapInstance || !isLoaded.value || !lifecycle.isActive(mapInstance)) {
      return
    }

    try {
      const source = mapInstance.getSource(SOURCE) as GeoJSONSource | undefined
      source?.setData(stopsToGeoJSON(newStops))
    } catch (error) {
      lifecycle.fail(error, mapInstance)
    }
  },
)

watch(
  () => props.selectedStopName,
  (name) => {
    const mapInstance = map.value
    if (!mapInstance || !isLoaded.value || !lifecycle.isActive(mapInstance)) {
      return
    }

    try {
      if (mapInstance.getLayer(LAYER_SELECTED)) {
        mapInstance.setFilter(
          LAYER_SELECTED,
          name ? ["==", ["get", "name"], name] : ["literal", false],
        )
      }
    } catch (error) {
      lifecycle.fail(error, mapInstance)
    }
  },
)
</script>

<template />
