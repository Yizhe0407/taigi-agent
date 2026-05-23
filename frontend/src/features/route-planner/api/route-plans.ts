import { apiBaseUrl, ApiError, parseErrorBody } from "@/lib/api"

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
  let response: Response
  try {
    response = await fetch(`${apiBaseUrl}/api/route-plans`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        destination: {
          lat: destination[1],
          lng: destination[0],
        },
        ...(departureTime
          ? { departureTime: departureTime.toISOString() }
          : {}),
      }),
      signal,
    })
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") {
      throw error
    }
    throw new RoutePlanApiError("目前無法連到路線規劃服務")
  }

  if (!response.ok) {
    throw new RoutePlanApiError(await parseErrorBody(response), response.status)
  }

  return (await response.json()) as RoutePlan
}
