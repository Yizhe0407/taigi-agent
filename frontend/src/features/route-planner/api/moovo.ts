import { apiFetch, ApiError } from "@/lib/api"

import type { MoovoStation } from "../types"

type MoovoStationsResponse = { stations: MoovoStation[] }

export class MoovoApiError extends ApiError {
  constructor(message: string, status: number | null = null) {
    super(message, status)
    this.name = "MoovoApiError"
  }
}

export async function fetchMoovoStations(
  signal?: AbortSignal,
): Promise<MoovoStation[]> {
  const response = await apiFetch("/api/moovo/stations", {
    signal,
    errorClass: MoovoApiError,
    networkMessage: "目前無法連到 MOOVO 站點服務",
  })
  const payload = (await response.json()) as MoovoStationsResponse
  return payload.stations
}
