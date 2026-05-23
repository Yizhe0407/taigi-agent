import type { MoovoStation } from "../types"

type MoovoStationsResponse = {
  stations: MoovoStation[]
}

type MoovoFailure = {
  detail?: string
}

const configuredApiBase = import.meta.env.VITE_API_BASE_URL?.replace(/\/$/, "")
const apiBaseUrl = configuredApiBase ?? ""

export class MoovoApiError extends Error {
  readonly status: number | null

  constructor(message: string, status: number | null = null) {
    super(message)
    this.name = "MoovoApiError"
    this.status = status
  }
}

const moovoStationsUrl = () => `${apiBaseUrl}/api/moovo/stations`

const responseMessage = async (response: Response) => {
  try {
    const body = (await response.json()) as MoovoFailure
    if (body.detail) return body.detail
  } catch {
    // Use the HTTP status fallback when the upstream error is not JSON.
  }
  return `MOOVO API 回應 ${response.status}`
}

export async function fetchMoovoStations(
  signal?: AbortSignal,
): Promise<MoovoStation[]> {
  let response: Response
  try {
    response = await fetch(moovoStationsUrl(), { signal })
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") {
      throw error
    }
    throw new MoovoApiError("目前無法連到 MOOVO 站點服務")
  }

  if (!response.ok) {
    throw new MoovoApiError(await responseMessage(response), response.status)
  }

  const payload = (await response.json()) as MoovoStationsResponse
  return payload.stations
}
