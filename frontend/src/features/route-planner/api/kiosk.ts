import type { LngLat, PlaceCoordinate } from "../types"

const configuredApiBase = import.meta.env.VITE_API_BASE_URL?.replace(/\/$/, "")
const apiBaseUrl = configuredApiBase ?? ""

type KioskResponse = { name: string; lat: number; lng: number }

/**
 * Fetch the kiosk stop name and coordinates from the backend.
 * The backend derives these from the stop catalog (same source OTP uses),
 * so the marker and route origin stay in sync.
 */
export async function fetchKiosk(): Promise<PlaceCoordinate> {
  const response = await fetch(`${apiBaseUrl}/api/kiosk`)
  if (!response.ok) throw new Error(`kiosk API ${response.status}`)
  const body = (await response.json()) as KioskResponse
  return {
    name: body.name,
    coordinates: [body.lng, body.lat] as LngLat,
  }
}
