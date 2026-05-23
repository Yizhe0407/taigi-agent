<script setup lang="ts">
import { NavigationControl } from "maplibre-gl"
import { watch } from "vue"

import { useMap } from "@/components/ui/map"

type ControlPosition = "top-left" | "top-right" | "bottom-left" | "bottom-right"

const props = withDefaults(
  defineProps<{
    position?: ControlPosition
  }>(),
  { position: "bottom-right" },
)

const { map } = useMap()

watch(
  map,
  (mapInstance, _, onCleanup) => {
    if (!mapInstance) return

    // Kiosk doesn't rotate the map, so the compass would always point north
    // and just add a redundant button. Visual zoom is the only thing elderly
    // touchscreen users can't easily do with pinch.
    const control = new NavigationControl({
      showCompass: false,
      showZoom: true,
      visualizePitch: false,
    })
    mapInstance.addControl(control, props.position)
    onCleanup(() => mapInstance.removeControl(control))
  },
  { immediate: true },
)
</script>

<template></template>
