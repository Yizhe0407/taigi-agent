import { afterEach, describe, expect, it, vi } from "vitest"

import {
  ApiError,
  apiFetch,
  readBlobResponse,
  readJsonResponse,
} from "@/lib/api"

class TestApiError extends ApiError {}

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe("abort-authoritative API operations", () => {
  it("keeps abort authoritative when fetch rejects generically after cancellation", async () => {
    const physicalFetch = deferred<Response>()
    vi.spyOn(globalThis, "fetch").mockReturnValue(physicalFetch.promise)
    const controller = new AbortController()
    const reason = new DOMException("request cancelled", "AbortError")

    const request = apiFetch("/api/test", {
      errorClass: TestApiError,
      networkMessage: "network failed",
      signal: controller.signal,
    })
    controller.abort(reason)
    physicalFetch.reject(new Error("late transport failure"))

    await expect(request).rejects.toBe(reason)
  })

  it("does not publish JSON that finishes parsing after cancellation", async () => {
    const body = deferred<unknown>()
    const response = { json: vi.fn(() => body.promise) } as unknown as Response
    const controller = new AbortController()
    const reason = new DOMException("JSON cancelled", "AbortError")

    const reading = readJsonResponse<{ value: string }>(response, controller.signal)
    controller.abort(reason)
    body.resolve({ value: "late" })

    await expect(reading).rejects.toBe(reason)
  })

  it("does not publish a Blob that finishes parsing after cancellation", async () => {
    const body = deferred<Blob>()
    const response = { blob: vi.fn(() => body.promise) } as unknown as Response
    const controller = new AbortController()
    const reason = new DOMException("Blob cancelled", "AbortError")

    const reading = readBlobResponse(response, controller.signal)
    controller.abort(reason)
    body.resolve(new Blob(["late"]))

    await expect(reading).rejects.toBe(reason)
  })

  it("does not downgrade cancellation during an error body read to a typed HTTP error", async () => {
    const body = deferred<unknown>()
    vi.spyOn(globalThis, "fetch").mockResolvedValue({
      ok: false,
      status: 503,
      json: vi.fn(() => body.promise),
    } as unknown as Response)
    const controller = new AbortController()
    const reason = new DOMException("error body cancelled", "AbortError")

    const request = apiFetch("/api/test", {
      errorClass: TestApiError,
      networkMessage: "network failed",
      signal: controller.signal,
    })
    await Promise.resolve()
    controller.abort(reason)
    body.resolve({ detail: "late failure" })

    await expect(request).rejects.toBe(reason)
  })
})
