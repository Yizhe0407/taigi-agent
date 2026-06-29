import type { Map as MapLibreMap } from "maplibre-gl"

import type { MapViewport } from "./types"

export function getViewport(map: MapLibreMap): MapViewport {
  const center = map.getCenter()
  return {
    center: [center.lng, center.lat],
    zoom: map.getZoom(),
    bearing: map.getBearing(),
    pitch: map.getPitch(),
  }
}
