export type DepartureSection =
  | "available"
  | "not_departed"
  | "last_departed"
  | "unknown"

export type DepartureDecision =
  | "arriving_soon"
  | "can_wait"
  | "long_wait"
  | "not_departed"
  | "last_departed"
  | "unknown"

export type DepartureSummary = {
  availableCount: number
  notDepartedCount: number
  lastDepartedCount: number
  unknownCount: number
}

export type DepartureRouteStatus = {
  id: string
  route: string
  routeId: string
  direction: string
  goBack: number
  section: DepartureSection
  decision: DepartureDecision
  statusText: string
  decisionText: string
  minutes: number | null
  scheduledTime: string | null
  carId: string | null
}

export type StopDepartureSnapshot = {
  stopName: string
  directionFilter: number | null
  updatedAt: string
  summary: DepartureSummary
  routes: DepartureRouteStatus[]
}

export type RouteStopDetail = {
  seq: number
  name: string
  isCurrentStop: boolean
  statusText: string
  minutes: number | null
  scheduledTime: string | null
}

export type RouteDirectionDetail = {
  goBack: number
  label: string
  stops: RouteStopDetail[]
}

export type DepartureRouteDetail = {
  route: string
  routeId: string
  stopName: string
  directionFilter: number | null
  directions: RouteDirectionDetail[]
}
