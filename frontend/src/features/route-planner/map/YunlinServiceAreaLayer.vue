<script setup lang="ts">
import { useMap } from "@/components/ui/map"
import { useMapLayer } from "@/components/ui/map/composables/use-map-layer"

import {
  yunlinBoundaryGeoJson,
  yunlinOutsideMaskGeoJson,
} from "../geo/yunlin-service-area"

const { map, isLoaded } = useMap()

const MASK_SOURCE_ID = "yunlin-service-area-mask-source"
const MASK_LAYER_ID = "yunlin-service-area-mask-layer"
const BOUNDARY_SOURCE_ID = "yunlin-service-area-boundary-source"
const BOUNDARY_GLOW_LAYER_ID = "yunlin-service-area-boundary-glow-layer"
const BOUNDARY_CASING_LAYER_ID = "yunlin-service-area-boundary-casing-layer"
const BOUNDARY_LAYER_ID = "yunlin-service-area-boundary-layer"

useMapLayer(map, isLoaded, (mapInstance) => {
  // ── Mask ────────────────────────────────────────────────────────────────
  // Cool blue-gray (#d1dce4) instead of near-white:
  //   - Creates visible contrast against the white boundary line on light maps
  //   - Still reads as a calm "out of service" fog, not a harsh dark overlay
  //   - On dark-matter the outside becomes a muted cool-gray surface that
  //     clearly differs from the dark interior cartography
  mapInstance.addSource(MASK_SOURCE_ID, {
    type: "geojson",
    data: yunlinOutsideMaskGeoJson,
  })
  mapInstance.addLayer({
    id: MASK_LAYER_ID,
    type: "fill",
    source: MASK_SOURCE_ID,
    paint: {
      "fill-color": "#d1dce4",
      "fill-opacity": 0.82,
    },
  })

  // ── Boundary (3 layers, bottom → top) ───────────────────────────────────
  mapInstance.addSource(BOUNDARY_SOURCE_ID, {
    type: "geojson",
    data: yunlinBoundaryGeoJson,
  })

  // 1. Glow — solid (not dashed), wide, blurred.
  //    Dashed + blur would smear into a continuous band; keep this solid so
  //    the halo reads as a smooth luminous aura around the dashed crisp line.
  mapInstance.addLayer({
    id: BOUNDARY_GLOW_LAYER_ID,
    type: "line",
    source: BOUNDARY_SOURCE_ID,
    paint: {
      "line-color": "#ffffff",
      "line-width": 10,
      "line-blur": 7,
      "line-opacity": 0.3,
    },
  })

  // 2. Casing — solid dark, medium width.
  //    Stays solid so the dash gaps in the crisp layer above reveal a
  //    continuous dark ring — gives dashes a natural "contained" framing.
  mapInstance.addLayer({
    id: BOUNDARY_CASING_LAYER_ID,
    type: "line",
    source: BOUNDARY_SOURCE_ID,
    paint: {
      "line-color": "#334155",
      "line-width": 4,
      "line-opacity": 0.35,
    },
  })

  // 3. Crisp — white dashed, narrow.
  //    Dashes convey "soft/advisory boundary" in cartographic convention,
  //    vs solid lines which imply hard walls or country borders.
  //    [6, 5]: 6px dash, 5px gap — visible but not busy at zoom 10-14.
  mapInstance.addLayer({
    id: BOUNDARY_LAYER_ID,
    type: "line",
    source: BOUNDARY_SOURCE_ID,
    paint: {
      "line-color": "#ffffff",
      "line-width": 2,
      "line-opacity": 1,
      "line-dasharray": [6, 5],
    },
  })

  return () => {
    try {
      for (const id of [
        BOUNDARY_LAYER_ID,
        BOUNDARY_CASING_LAYER_ID,
        BOUNDARY_GLOW_LAYER_ID,
        MASK_LAYER_ID,
      ]) {
        if (mapInstance.getLayer(id)) mapInstance.removeLayer(id)
      }
      for (const id of [BOUNDARY_SOURCE_ID, MASK_SOURCE_ID]) {
        if (mapInstance.getSource(id)) mapInstance.removeSource(id)
      }
    } catch {
      // MapLibre silently drops custom layers when the basemap style changes.
    }
  }
})
</script>

<template></template>
