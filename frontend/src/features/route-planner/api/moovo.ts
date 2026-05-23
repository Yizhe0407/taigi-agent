import { apiBaseUrl, ApiError, parseErrorBody } from "@/lib/api"

import type { MoovoStation } from "../types"

type MoovoStationsResponse = {
  stations: MoovoStation[]
}

export class MoovoApiError extends ApiError {
  constructor(message: string, status: number | null = null) {
    super(message, status)
    this.name = "MoovoApiError"
  }
}

export async function fetchMoovoStations(
  signal?: AbortSignal,
): Promise<MoovoStation[]> {
  let response: Response
  try {
    response = await fetch(`${apiBaseUrl}/api/moovo/stations`, { signal })
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") {
      throw error
    }
    throw new MoovoApiError("目前無法連到 MOOVO 站點服務")
  }

  if (!response.ok) {
    throw new MoovoApiError(await parseErrorBody(response), response.status)
  }

  const payload = (await response.json()) as MoovoStationsResponse
  return payload.stations
}
