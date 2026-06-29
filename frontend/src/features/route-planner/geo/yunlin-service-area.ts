import yunlinCounty from "@/assets/geo/yunlin-county.json"

import type { LngLat } from "../types"

type LinearRing = LngLat[]
type PolygonCoordinates = LinearRing[]
type MultiPolygonCoordinates = PolygonCoordinates[]

type YunlinBoundaryFeature = {
  type: "Feature"
  properties: {
    name: string
    countyName: string
    countyCode: string
  }
  geometry: {
    type: "MultiPolygon"
    coordinates: MultiPolygonCoordinates
  }
}

type FeatureCollection = {
  type: "FeatureCollection"
  features: YunlinBoundaryFeature[]
}

type MaskFeatureCollection = {
  type: "FeatureCollection"
  features: Array<{
    type: "Feature"
    properties: {
      name: string
    }
    geometry: {
      type: "Polygon"
      coordinates: LinearRing[]
    }
  }>
}

const boundary = yunlinCounty as unknown as FeatureCollection
const [yunlinFeature] = boundary.features
const polygons = yunlinFeature.geometry.coordinates

/**
 * Return the bounding-box area (in square degrees) of a ring.
 * Used to filter out offshore sandbars and tiny tidal islands that are
 * administratively part of Yunlin County but unreachable by bus — most
 * notably 外傘頂洲 (Waisanding Sandbar, ~0.009 sq°) off the coast of 口湖鄉.
 */
const ringBBoxArea = (ring: LinearRing): number => {
  let minLng = Infinity, maxLng = -Infinity, minLat = Infinity, maxLat = -Infinity
  for (const [lng, lat] of ring) {
    if (lng < minLng) minLng = lng
    if (lng > maxLng) maxLng = lng
    if (lat < minLat) minLat = lat
    if (lat > maxLat) maxLat = lat
  }
  return (maxLng - minLng) * (maxLat - minLat)
}

/**
 * Only count polygons whose outer-ring bounding box exceeds this threshold.
 * At 0.01 sq° only the main Yunlin landmass (0.207 sq°) passes; all
 * offshore fragments including 外傘頂洲 (0.009 sq°) are excluded.
 */
const MIN_BBOX_AREA_SQ_DEG = 0.01

const mainlandPolygons = polygons.filter(
  (poly) => poly[0] && ringBBoxArea(poly[0]) >= MIN_BBOX_AREA_SQ_DEG,
)

const WORLD_MASK_RING: LinearRing = [
  [-180, -85],
  [180, -85],
  [180, 85],
  [-180, 85],
  [-180, -85],
]

const pointOnSegment = (point: LngLat, start: LngLat, end: LngLat) => {
  const [x, y] = point
  const [x1, y1] = start
  const [x2, y2] = end
  const cross = (x - x1) * (y2 - y1) - (y - y1) * (x2 - x1)
  if (Math.abs(cross) > 1e-10) return false

  return (
    x >= Math.min(x1, x2) - 1e-10 &&
    x <= Math.max(x1, x2) + 1e-10 &&
    y >= Math.min(y1, y2) - 1e-10 &&
    y <= Math.max(y1, y2) + 1e-10
  )
}

const pointInRing = (point: LngLat, ring: LinearRing) => {
  const [x, y] = point
  let inside = false

  for (let current = 0, previous = ring.length - 1; current < ring.length; previous = current++) {
    const start = ring[previous]
    const end = ring[current]
    if (pointOnSegment(point, start, end)) return true

    const [x1, y1] = start
    const [x2, y2] = end
    const intersects =
      y1 > y !== y2 > y && x < ((x2 - x1) * (y - y1)) / (y2 - y1) + x1
    if (intersects) inside = !inside
  }

  return inside
}

const pointInPolygon = (point: LngLat, polygon: PolygonCoordinates) => {
  const [outerRing, ...holes] = polygon
  if (!outerRing || !pointInRing(point, outerRing)) return false
  return !holes.some((hole) => pointInRing(point, hole))
}

export const isInYunlinCounty = (coordinates: LngLat) =>
  mainlandPolygons.some((polygon) => pointInPolygon(coordinates, polygon))

export const yunlinBoundaryGeoJson: FeatureCollection = {
  type: "FeatureCollection",
  features: [
    {
      ...yunlinFeature,
      geometry: {
        type: "MultiPolygon",
        coordinates: mainlandPolygons,
      },
    },
  ],
}

export const yunlinOutsideMaskGeoJson: MaskFeatureCollection = {
  type: "FeatureCollection",
  features: [
    {
      type: "Feature",
      properties: {
        name: "雲林縣服務範圍外遮罩",
      },
      geometry: {
        type: "Polygon",
        coordinates: [
          WORLD_MASK_RING,
          ...mainlandPolygons.map((polygon) => polygon[0]).filter(Boolean),
        ],
      },
    },
  ],
}
