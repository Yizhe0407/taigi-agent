import type { LngLat, RoutePlan } from "../types"

type RoutePlanFailure = {
  detail?: string
}

const configuredApiBase = import.meta.env.VITE_API_BASE_URL?.replace(/\/$/, "")
const apiBaseUrl = configuredApiBase ?? ""

export class RoutePlanApiError extends Error {
  readonly status: number | null

  constructor(message: string, status: number | null = null) {
    super(message)
    this.name = "RoutePlanApiError"
    this.status = status
  }
}

const routePlanUrl = () => `${apiBaseUrl}/api/route-plans`

const responseMessage = async (response: Response) => {
  try {
    const body = (await response.json()) as RoutePlanFailure
    if (body.detail) return body.detail
  } catch {
    // Prefer the HTTP status fallback when an upstream error is not JSON.
  }
  return `路線規劃 API 回應 ${response.status}`
}

export async function createRoutePlan(
  destination: LngLat,
  departureTime?: Date,
  signal?: AbortSignal,
): Promise<RoutePlan> {
  let response: Response
  try {
    response = await fetch(routePlanUrl(), {
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
    throw new RoutePlanApiError(await responseMessage(response), response.status)
  }

  return (await response.json()) as RoutePlan
}
