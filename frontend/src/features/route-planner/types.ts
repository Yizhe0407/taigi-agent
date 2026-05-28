export type LngLat = [lng: number, lat: number]

export type PlaceCoordinate = {
  name: string
  coordinates: LngLat
}

/** Kiosk stop with its current direction filter (去程 / 回程 / null = both). */
export type KioskPlace = PlaceCoordinate & {
  direction: "去程" | "回程" | null
}

export type RoutePlace = {
  name: string
  lat: number
  lng: number
}

export type RouteName = {
  shortName: string | null
  longName: string | null
}

export type RouteLeg = {
  mode: string
  fromName: string
  toName: string
  start: string
  end: string
  duration: number
  distance: number
  coordinates: LngLat[]
  route: RouteName | null
}

export type RouteOption = {
  id: string
  coordinates: LngLat[]
  duration: number
  distance: number
  transferCount: number
  legs: RouteLeg[]
}

export type RoutePlan = {
  origin: RoutePlace
  destination: RoutePlace
  routes: RouteOption[]
}

export type MoovoStation = {
  stationUid: string
  stationId: string | null
  name: string
  lat: number
  lng: number
  bikeCapacity: number
  availableRentBikes: number
  availableReturnBikes: number
  serviceStatus: number
  updateTime: string | null
}
