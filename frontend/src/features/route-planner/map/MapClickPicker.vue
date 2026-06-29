<script setup lang="ts">
import type { MapMouseEvent } from "maplibre-gl"
import { watch } from "vue"

import { useMap } from "@/components/ui/map"

import { isInYunlinCounty } from "../geo/yunlin-service-area"
import type { LngLat } from "../types"

const emit = defineEmits<{
  select: [coordinates: LngLat]
  reject: [coordinates: LngLat]
}>()

const { map } = useMap()

watch(
  map,
  (mapInstance, _, onCleanup) => {
    if (!mapInstance) return

    const handleClick = (event: MapMouseEvent) => {
      const coordinates: LngLat = [event.lngLat.lng, event.lngLat.lat]
      if (!isInYunlinCounty(coordinates)) {
        emit("reject", coordinates)
        return
      }
      emit("select", coordinates)
    }

    mapInstance.on("click", handleClick)
    onCleanup(() => mapInstance.off("click", handleClick))
  },
  { immediate: true },
)
</script>

<template></template>
