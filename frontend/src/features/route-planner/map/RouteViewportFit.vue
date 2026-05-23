<script setup lang="ts">
import { LngLatBounds } from "maplibre-gl"
import { watch } from "vue"

import { useMap } from "@/components/ui/map"

import type { LngLat } from "../types"

const props = defineProps<{
  coordinates: LngLat[]
}>()

const { map, isLoaded } = useMap()

watch(
  [map, isLoaded, () => props.coordinates],
  ([mapInstance, mapIsLoaded, coordinates]) => {
    if (!mapInstance || !mapIsLoaded || coordinates.length < 2) return

    const [first, ...remaining] = coordinates
    const bounds = new LngLatBounds(first, first)
    for (const coordinate of remaining) bounds.extend(coordinate)

    mapInstance.fitBounds(bounds, {
      duration: 500,
      maxZoom: 15,
      padding: { top: 72, right: 56, bottom: 72, left: 56 },
    })
  },
  { deep: true, immediate: true },
)
</script>

<template></template>
