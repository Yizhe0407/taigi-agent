import type {
  LineLayerSpecification,
  Map as MapLibreMap,
} from "maplibre-gl"

export type Theme = "light" | "dark"

export type MapViewport = {
  center: [number, number]
  zoom: number
  bearing: number
  pitch: number
}

export type MapRef = MapLibreMap

export type MapArcLinePaint = NonNullable<LineLayerSpecification["paint"]>
