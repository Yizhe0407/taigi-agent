import { apiFetch, ApiError } from "@/lib/api"
import { API_NETWORK_MESSAGES } from "@/lib/api-messages"

import type { KioskPlace, LngLat } from "../types"

export class KioskApiError extends ApiError {
  constructor(message: string, status: number | null = null) {
    super(message, status)
    this.name = "KioskApiError"
  }
}

type KioskResponse = { name: string; lat: number; lng: number; direction: string | null }

/**
 * Fetch the kiosk stop name, coordinates, and direction from the backend.
 * The backend derives coordinates from the stop catalog (same source OTP uses),
 * so the marker and route origin stay in sync.
 */
export async function fetchKiosk(): Promise<KioskPlace> {
  const response = await apiFetch("/api/kiosk", {
    errorClass: KioskApiError,
    networkMessage: API_NETWORK_MESSAGES.kiosk,
  })
  const body = (await response.json()) as KioskResponse
  const VALID_DIRECTIONS = new Set<string>(["去程", "回程"])
  const direction: KioskPlace["direction"] =
    body.direction !== null && VALID_DIRECTIONS.has(body.direction)
      ? (body.direction as KioskPlace["direction"])
      : null
  return { name: body.name, coordinates: [body.lng, body.lat] as LngLat, direction }
}
