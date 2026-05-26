import { apiFetch, ApiError } from "@/lib/api"

import type { LngLat, RoutePlan } from "../types"

export class RoutePlanApiError extends ApiError {
  constructor(message: string, status: number | null = null) {
    super(message, status)
    this.name = "RoutePlanApiError"
  }
}

export async function createRoutePlan(
  destination: LngLat,
  departureTime?: Date,
  signal?: AbortSignal,
): Promise<RoutePlan> {
  const response = await apiFetch("/api/route-plans", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      destination: { lat: destination[1], lng: destination[0] },
      ...(departureTime ? { departureTime: departureTime.toISOString() } : {}),
    }),
    signal,
    errorClass: RoutePlanApiError,
    networkMessage: "目前無法連到路線規劃服務",
  })
  return (await response.json()) as RoutePlan
}
