import { API_NETWORK_MESSAGES } from "@/lib/api-messages"
import { apiFetch, ApiError } from "@/lib/api"

import type { DepartureRouteDetail, StopDepartureSnapshot } from "../types"

export class DeparturesApiError extends ApiError {
  constructor(message: string, status: number | null = null) {
    super(message, status)
    this.name = "DeparturesApiError"
  }
}

export async function fetchDeparturesHere(
  signal?: AbortSignal,
): Promise<StopDepartureSnapshot> {
  const response = await apiFetch("/api/departures/here", {
    signal,
    errorClass: DeparturesApiError,
    networkMessage: API_NETWORK_MESSAGES.departures,
  })
  return (await response.json()) as StopDepartureSnapshot
}

export async function fetchDepartureRouteDetail(
  route: string,
  signal?: AbortSignal,
): Promise<DepartureRouteDetail> {
  const response = await apiFetch(
    `/api/departures/routes/${encodeURIComponent(route)}/detail`,
    {
      signal,
      errorClass: DeparturesApiError,
      networkMessage: API_NETWORK_MESSAGES.routeDetail,
    },
  )
  return (await response.json()) as DepartureRouteDetail
}
