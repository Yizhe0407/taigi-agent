import type { DepartureRouteStatus } from "../types"

export type DepartureDisplayState = "boarding" | "pending" | "expired"

export function departureDisplayState(
  route: DepartureRouteStatus,
): DepartureDisplayState {
  if (route.section === "available") return "boarding"
  if (route.section === "not_departed") return "pending"
  return "expired"
}

export function departureMinutesLabel(route: DepartureRouteStatus): string {
  if (route.minutes === null) return route.statusText
  if (route.minutes <= 0) return "即將到站"
  return `${route.minutes} 分`
}

export function heroStatusState(
  route: DepartureRouteStatus | null,
): DepartureDisplayState {
  if (!route) return "pending"
  if (route.decision === "not_departed" || route.decision === "scheduled") {
    return "pending"
  }
  if (route.decision === "last_departed" || route.decision === "unknown") {
    return "expired"
  }
  return "boarding"
}

export function heroStatusText(route: DepartureRouteStatus | null): string {
  return route?.decisionText ?? "無可搭班次"
}

/** Tailwind chip + dot classes per departure state. Single source of truth so hero and route list stay in sync. */
export function statusChipClasses(state: DepartureDisplayState): {
  chip: string
  dot: string
} {
  if (state === "boarding")
    return { chip: "bg-kiosk-ok-soft text-kiosk-ok", dot: "bg-kiosk-ok" }
  if (state === "pending")
    return { chip: "bg-kiosk-info-soft text-kiosk-info", dot: "bg-kiosk-info" }
  return { chip: "bg-kiosk-dim text-kiosk-faded", dot: "bg-kiosk-faded" }
}
