import { beforeEach, describe, expect, it, vi } from "vitest"

import {
  AdminApiError,
  fetchAdminKiosk,
  updateAdminKiosk,
} from "@/features/admin/api/admin"
import { apiFetch } from "@/lib/api"

vi.mock("@/lib/api", () => {
  class ApiError extends Error {
    constructor(message: string, readonly status: number | null = null) {
      super(message)
    }
  }
  return {
    apiFetch: vi.fn(),
    readJsonResponse: async <T,>(response: Response, signal?: AbortSignal) => {
      signal?.throwIfAborted()
      const body = (await response.json()) as T
      signal?.throwIfAborted()
      return body
    },
    readBlobResponse: async (response: Response, signal?: AbortSignal) => {
      signal?.throwIfAborted()
      const body = await response.blob()
      signal?.throwIfAborted()
      return body
    },
    ApiError,
  }
})

type Deferred<T> = {
  promise: Promise<T>
  resolve: (value: T) => void
  reject: (reason?: unknown) => void
}

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, resolve, reject }
}

describe("admin API cancellation", () => {
  let prompt: ReturnType<typeof vi.fn>

  beforeEach(() => {
    vi.mocked(apiFetch).mockReset()
    sessionStorage.clear()
    prompt = vi.fn(() => null)
    Object.defineProperty(window, "prompt", {
      configurable: true,
      value: prompt,
    })
  })

  it("does not create an HTTP request or prompt for an already aborted update", async () => {
    const controller = new AbortController()
    controller.abort()

    await expect(
      updateAdminKiosk(
        { stop_name: "stop", direction: "回程" },
        controller.signal,
      ),
    ).rejects.toMatchObject({ name: "AbortError" })

    expect(apiFetch).not.toHaveBeenCalled()
    expect(prompt).not.toHaveBeenCalled()
  })

  it("does not prompt or retry when the request is aborted before a late 401 rejection", async () => {
    const request = deferred<Response>()
    vi.mocked(apiFetch).mockReturnValue(request.promise)
    const controller = new AbortController()

    const update = updateAdminKiosk(
      { stop_name: "stop", direction: "回程" },
      controller.signal,
    )
    controller.abort()
    request.reject(new AdminApiError("unauthorized", 401))

    await expect(update).rejects.toMatchObject({ name: "AbortError" })
    expect(apiFetch).toHaveBeenCalledOnce()
    expect(prompt).not.toHaveBeenCalled()
  })

  it("stops body parsing from publishing a response aborted in flight", async () => {
    const body = deferred<{
      stop_name: string
      direction: "回程"
      lat: number
      lng: number
    }>()
    const json = vi.fn(() => body.promise)
    vi.mocked(apiFetch).mockResolvedValue({ json } as unknown as Response)
    const controller = new AbortController()

    const request = fetchAdminKiosk(controller.signal)
    await vi.waitFor(() => expect(json).toHaveBeenCalledOnce())
    controller.abort()
    body.resolve({
      stop_name: "late",
      direction: "回程",
      lat: 23.7,
      lng: 120.5,
    })

    await expect(request).rejects.toMatchObject({ name: "AbortError" })
  })
})
