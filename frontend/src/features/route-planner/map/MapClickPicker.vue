<script setup lang="ts">
import type { MapMouseEvent } from "maplibre-gl"
import { watch } from "vue"

import { useMap } from "@/components/ui/map"

import type { LngLat } from "../types"

const emit = defineEmits<{
  select: [coordinates: LngLat]
}>()

const { map } = useMap()

watch(
  map,
  (mapInstance, _, onCleanup) => {
    if (!mapInstance) return

    const handleClick = (event: MapMouseEvent) => {
      emit("select", [event.lngLat.lng, event.lngLat.lat])
    }

    mapInstance.on("click", handleClick)
    onCleanup(() => mapInstance.off("click", handleClick))
  },
  { immediate: true },
)
</script>

<template></template>
