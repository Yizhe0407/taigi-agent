/**
 * Shared API utilities used across all feature API clients.
 *
 * - `apiBaseUrl`     — resolved once from VITE_API_BASE_URL; empty string = same origin
 * - `ApiError`       — base class for typed HTTP errors; feature files extend this so
 *                      `instanceof ChatApiError` / `instanceof RoutePlanApiError` keep working
 * - `parseErrorBody` — reads `{ detail }` from a failed response, falls back to HTTP status
 */

const configured = import.meta.env.VITE_API_BASE_URL?.replace(/\/$/, "")

/** Base URL for all backend API calls. Empty string means same origin. */
export const apiBaseUrl: string = configured ?? ""

/**
 * Base class for API errors that carry an HTTP status code.
 * Extend this in each feature's API client rather than duplicating the
 * `status: number | null` field and constructor.
 */
export class ApiError extends Error {
  readonly status: number | null

  constructor(message: string, status: number | null = null) {
    super(message)
    this.name = "ApiError"
    this.status = status
  }
}

/**
 * Extract a human-readable message from a non-ok API response.
 * Prefers `response.body.detail` (FastAPI error format); falls back to the HTTP status.
 */
export async function parseErrorBody(response: Response): Promise<string> {
  try {
    const body = (await response.json()) as { detail?: string }
    if (body.detail) return body.detail
  } catch {
    // non-JSON body — fall through to HTTP status fallback
  }
  return `API 回應 ${response.status}`
}
