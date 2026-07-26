import { computed, type MaybeRefOrGetter, toValue } from "vue"

import {
  buildRouteColorAssignments,
  normalizeRouteCode,
  routeColorBgClass,
  routeColorBorderClass,
  routeColorTextClass,
  type RouteColorToken,
} from "./route-colors"

export function useRouteColors(routeCodes: MaybeRefOrGetter<string[]>) {
  // Memoize on the route-code *set*, not the array reference: kiosk snapshots
  // reorder routes every refresh (ETA-sorted) without changing which routes
  // are visible, and buildRouteColorAssignments() is order-sensitive in its
  // tie-breaking — recomputing on every reorder would both reshuffle colors
  // pointlessly and hand downstream consumers a new object reference each
  // tick, forcing a full re-render. Same code set -> same cached object.
  let cachedKey = ""
  let cachedAssignments: Record<string, RouteColorToken> = {}

  const assignments = computed<Record<string, RouteColorToken>>(() => {
    const codes = toValue(routeCodes)
    const key = Array.from(new Set(codes.map(normalizeRouteCode).filter(Boolean)))
      .sort()
      .join(",")
    if (key !== cachedKey) {
      cachedKey = key
      cachedAssignments = buildRouteColorAssignments(codes)
    }
    return cachedAssignments
  })

  function getRouteBgClass(routeCode: string): string {
    return assignments.value[normalizeRouteCode(routeCode)]?.bgClass ?? routeColorBgClass(routeCode)
  }

  function getRouteBorderClass(routeCode: string): string {
    return assignments.value[normalizeRouteCode(routeCode)]?.borderClass ?? routeColorBorderClass(routeCode)
  }

  function getRouteTextClass(routeCode: string): string {
    return assignments.value[normalizeRouteCode(routeCode)]?.textClass ?? routeColorTextClass(routeCode)
  }

  return {
    assignments,
    getRouteBgClass,
    getRouteBorderClass,
    getRouteTextClass,
  }
}
