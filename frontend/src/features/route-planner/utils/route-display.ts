import { routeColor } from "@/lib/route-colors"

import type { LngLat, RouteLeg, RouteOption } from "../types"

const BUS_FALLBACK_COLOR = "#1F5BBF"
const WALK_COLOR = "#9C968A"

export function durationLabel(seconds: number): string {
  const minutes = Math.max(1, Math.round(seconds / 60))
  if (minutes < 60) return `${minutes} 分鐘`

  const hours = Math.floor(minutes / 60)
  const remainder = minutes % 60
  return remainder === 0 ? `${hours} 小時` : `${hours} 小時 ${remainder} 分`
}

export function distanceLabel(meters: number): string {
  return meters >= 1000
    ? `${(meters / 1000).toFixed(1)} km`
    : `${Math.round(meters)} m`
}

export function routePrimaryId(route: RouteOption): string {
  const busLeg = route.legs.find((leg) => leg.mode === "BUS")
  return busLeg?.route?.shortName ?? busLeg?.route?.longName ?? "公車"
}

export function routeBadgeColor(route: RouteOption): string {
  const primaryId = routePrimaryId(route)
  return primaryId === "公車" ? "#161412" : routeColor(primaryId)
}

export function legColor(leg: RouteLeg): string {
  if (leg.mode !== "BUS") return WALK_COLOR
  return leg.route?.shortName ? routeColor(leg.route.shortName) : BUS_FALLBACK_COLOR
}

export function walkMinsLabel(route: RouteOption): string {
  const seconds = route.legs
    .filter((leg) => leg.mode === "WALK")
    .reduce((sum, leg) => sum + leg.duration, 0)
  const minutes = Math.max(0, Math.round(seconds / 60))
  return minutes > 0 ? `步行 ${minutes} 分` : ""
}

export function transferLabel(route: RouteOption): string {
  return route.transferCount === 0
    ? "不用轉乘"
    : `轉乘 ${route.transferCount} 次`
}

export function routeSecondaryLabel(route: RouteOption): string {
  const walkLabel = walkMinsLabel(route)
  return walkLabel ? `${transferLabel(route)} · ${walkLabel}` : transferLabel(route)
}

export function legDisplayCoordinates(route: RouteOption, index: number): LngLat[] {
  const leg = route.legs[index]
  if (leg.coordinates.length > 1) return leg.coordinates
  if (leg.mode !== "WALK") return leg.coordinates

  const prev = index > 0 ? route.legs[index - 1] : null
  const next = index < route.legs.length - 1 ? route.legs[index + 1] : null
  const start = prev?.coordinates.at(-1)
  const end = next?.coordinates[0]

  if (start && end && (start[0] !== end[0] || start[1] !== end[1])) {
    return [start, end]
  }
  return leg.coordinates
}
