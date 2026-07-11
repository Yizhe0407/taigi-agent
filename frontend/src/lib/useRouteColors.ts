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
  const assignments = computed<Record<string, RouteColorToken>>(() =>
    buildRouteColorAssignments(toValue(routeCodes)),
  )

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
