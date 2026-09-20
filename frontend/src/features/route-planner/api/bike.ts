import { API_NETWORK_MESSAGES } from "@/lib/api-messages"
import { apiFetch, ApiError, readJsonResponse } from "@/lib/api"

import type { BikeStation } from "../types"

type BikeStationsResponse = { stations: BikeStation[] }

export class BikeApiError extends ApiError {
  constructor(message: string, status: number | null = null) {
    super(message, status)
    this.name = "BikeApiError"
  }
}

export async function fetchBikeStations(
  signal?: AbortSignal,
): Promise<BikeStation[]> {
  const response = await apiFetch("/api/bike/stations", {
    signal,
    errorClass: BikeApiError,
    networkMessage: API_NETWORK_MESSAGES.bike,
  })
  const payload = await readJsonResponse<BikeStationsResponse>(response, signal)
  return payload.stations
}
