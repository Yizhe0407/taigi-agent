/**
 * Shared API utilities used across all feature API clients.
 *
 * - `apiBaseUrl`     — resolved once from VITE_API_BASE_URL; empty string = same origin
 * - `ApiError`       — base class for typed HTTP errors; feature files extend this so
 *                      `instanceof ChatApiError` / `instanceof RoutePlanApiError` keep working
 * - `readJsonResponse` / `readBlobResponse` — abort-gated body readers
 * - `parseErrorBody` — reads `{ detail }` from a failed response, falls back to HTTP status
 * - `apiFetch`       — wraps fetch with: base URL, network-error → typed error,
 *                      non-ok → typed error (with status + parsed detail), abort untouched.
 *                      Returns the raw ok `Response`; callers pick `.json()` / `.blob()`.
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
type RequestSignal = AbortSignal | null | undefined

function throwIfAborted(signal: RequestSignal): void {
  signal?.throwIfAborted()
}

/** Read a JSON body without allowing an abort that races body parsing to publish data. */
export async function readJsonResponse<T>(
  response: Response,
  signal?: RequestSignal,
): Promise<T> {
  throwIfAborted(signal)
  const body = (await response.json()) as T
  throwIfAborted(signal)
  return body
}

/** Read a Blob body without allowing an abort that races body parsing to publish data. */
export async function readBlobResponse(
  response: Response,
  signal?: RequestSignal,
): Promise<Blob> {
  throwIfAborted(signal)
  const body = await response.blob()
  throwIfAborted(signal)
  return body
}

export async function parseErrorBody(
  response: Response,
  signal?: RequestSignal,
): Promise<string> {
  try {
    const body = await readJsonResponse<{ detail?: string }>(response, signal)
    if (body.detail) return body.detail
  } catch {
    // An abort remains authoritative; only malformed/non-JSON bodies fall back.
    throwIfAborted(signal)
  }
  return `API 回應 ${response.status}`
}

type ApiErrorCtor<E extends ApiError> = new (
  message: string,
  status?: number | null,
) => E

export interface ApiFetchOptions<E extends ApiError> extends RequestInit {
  /** Subclass of ApiError thrown for both network and non-ok responses. */
  errorClass: ApiErrorCtor<E>
  /** Human-readable message used when fetch itself rejects (DNS, offline, CORS). */
  networkMessage: string
}

/**
 * Wrap fetch with project-wide error handling. AbortError always propagates as-is;
 * everything else surfaces as `errorClass`. Returns the ok response — callers decide
 * how to read the body (`.json()`, `.blob()`, …).
 */
export async function apiFetch<E extends ApiError>(
  path: string,
  options: ApiFetchOptions<E>,
): Promise<Response> {
  const { errorClass: ErrorClass, networkMessage, ...init } = options
  const signal = init.signal
  throwIfAborted(signal)

  let response: Response
  try {
    response = await fetch(`${apiBaseUrl}${path}`, init)
    throwIfAborted(signal)
  } catch (error) {
    throwIfAborted(signal)
    if (error instanceof DOMException && error.name === "AbortError") throw error
    throw new ErrorClass(networkMessage)
  }
  if (!response.ok) {
    const message = await parseErrorBody(response, signal)
    throwIfAborted(signal)
    throw new ErrorClass(message, response.status)
  }
  return response
}
