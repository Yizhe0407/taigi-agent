<script setup lang="ts">
import type { MapMouseEvent } from "maplibre-gl"

import { useMap } from "@/components/ui/map"
import { useMapLayer } from "@/components/ui/map/composables/use-map-layer"

import { isInYunlinCounty } from "../geo/yunlin-service-area"
import type { LngLat } from "../types"

const emit = defineEmits<{
  select: [coordinates: LngLat]
  reject: [coordinates: LngLat]
}>()

const { map, isLoaded } = useMap()

useMapLayer(
  map,
  isLoaded,
  "map click picker",
  (mapInstance, owner, fail) => {
    const handleClick = (event: MapMouseEvent) => {
      if (owner.signal.aborted) return
      try {
        const coordinates: LngLat = [event.lngLat.lng, event.lngLat.lat]
        if (!isInYunlinCounty(coordinates)) {
          emit("reject", coordinates)
          return
        }
        emit("select", coordinates)
      } catch (error) {
        fail(error)
      }
    }

    owner.acquire(
      () => mapInstance.on("click", handleClick),
      () => mapInstance.off("click", handleClick),
    )
  },
)
</script>

<template></template>
