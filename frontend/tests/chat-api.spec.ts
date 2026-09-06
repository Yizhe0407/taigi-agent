import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import {
  ChatApiError,
  deleteChatSession,
  sendChatMessageStream,
} from "@/features/agent-chat/api/chat"
import { apiFetch } from "@/lib/api"
import { reportClientEvent } from "@/lib/report-client-event"

vi.mock("@/lib/api", () => {
  class ApiError extends Error {
    constructor(message: string, readonly status: number | null = null) {
      super(message)
    }
  }
  return {
    apiBaseUrl: "",
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

vi.mock("@/lib/report-client-event", () => ({
  reportClientEvent: vi.fn(),
}))

type TestReader = {
  read: () => Promise<ReadableStreamReadResult<Uint8Array<ArrayBuffer>>>
  cancel: () => Promise<void>
  releaseLock: () => void
}


function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}

function responseWithReader(
  reader: TestReader,
  cancelStream: () => Promise<void> = async () => {},
): Response {
  return {
    body: {
      getReader: () => reader,
      cancel: cancelStream,
    },
  } as unknown as Response
}

describe("chat stream reader ownership", () => {
  beforeEach(() => {
    vi.mocked(apiFetch).mockReset()
    vi.mocked(reportClientEvent).mockClear()
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it("releases the reader without cancelling after normal EOF", async () => {
    const read = vi.fn()
      .mockResolvedValueOnce({
        done: false,
        value: new TextEncoder().encode('data: {"delta":"答覆"}\n\n'),
      })
      .mockResolvedValueOnce({ done: true, value: undefined })
    const cancel = vi.fn(async () => {})
    const releaseLock = vi.fn()
    vi.mocked(apiFetch).mockResolvedValue(responseWithReader({ read, cancel, releaseLock }))
    const onDelta = vi.fn()

    await expect(sendChatMessageStream("session", "question", onDelta)).resolves.toBe("答覆")

    expect(onDelta).toHaveBeenCalledOnce()
    expect(onDelta).toHaveBeenCalledWith("答覆")
    expect(cancel).not.toHaveBeenCalled()
    expect(releaseLock).toHaveBeenCalledOnce()
  })

  it("cancels and releases the reader after early protocol completion", async () => {
    const read = vi.fn().mockResolvedValueOnce({
      done: false,
      value: new TextEncoder().encode('data: {"delta":"完成","done":true}\n\n'),
    })
    const cancel = vi.fn(async () => {})
    const releaseLock = vi.fn()
    vi.mocked(apiFetch).mockResolvedValue(responseWithReader({ read, cancel, releaseLock }))

    await expect(sendChatMessageStream("session", "question", vi.fn())).resolves.toBe("完成")

    expect(read).toHaveBeenCalledOnce()
    expect(cancel).toHaveBeenCalledOnce()
    expect(releaseLock).toHaveBeenCalledOnce()
    expect(reportClientEvent).not.toHaveBeenCalled()
  })

  it("retains a failed cancellation and retries the same stream after releasing its reader lock", async () => {
    const read = vi.fn().mockResolvedValueOnce({
      done: false,
      value: new TextEncoder().encode('data: {"done":true}\n\n'),
    })
    const cancelReader = vi.fn().mockRejectedValue(new Error("reader cancel failed"))
    const cancelStream = vi.fn(async () => {})
    const releaseLock = vi.fn()
    vi.mocked(apiFetch).mockResolvedValue(responseWithReader(
      { read, cancel: cancelReader, releaseLock },
      cancelStream,
    ))

    await expect(sendChatMessageStream("session", "question", vi.fn()))
      .rejects.toThrow("Chat stream cleanup debt remains")
    expect(cancelReader).toHaveBeenCalledOnce()
    expect(releaseLock).toHaveBeenCalledOnce()
    expect(cancelStream).not.toHaveBeenCalled()

    vi.spyOn(globalThis, "fetch").mockResolvedValue({ ok: true, status: 204 } as Response)
    await expect(deleteChatSession("session")).resolves.toBeUndefined()
    expect(cancelReader).toHaveBeenCalledOnce()
    expect(cancelStream).toHaveBeenCalledOnce()
    expect(releaseLock).toHaveBeenCalledOnce()
  })

  it("aggregates a primary stream failure with cleanup failure and retries only the failed action", async () => {
    const primaryFailure = new Error("reader failed")
    const read = vi.fn().mockRejectedValue(primaryFailure)
    const cancel = vi.fn(async () => {})
    const releaseLock = vi.fn()
      .mockImplementationOnce(() => {
        throw new Error("release failed")
      })
      .mockImplementation(() => {})
    vi.mocked(apiFetch).mockResolvedValue(responseWithReader({ read, cancel, releaseLock }))

    const operation = sendChatMessageStream("session", "question", vi.fn())
    await expect(operation).rejects.toBeInstanceOf(AggregateError)
    await operation.catch((error: AggregateError) => {
      expect(error.errors[0]).toBe(primaryFailure)
      expect(error.errors[1]).toBeInstanceOf(AggregateError)
    })
    expect(cancel).toHaveBeenCalledOnce()
    expect(releaseLock).toHaveBeenCalledOnce()

    vi.spyOn(globalThis, "fetch").mockResolvedValue({ ok: true, status: 204 } as Response)
    await expect(deleteChatSession("session")).resolves.toBeUndefined()
    expect(cancel).toHaveBeenCalledOnce()
    expect(releaseLock).toHaveBeenCalledTimes(2)
  })

  it("publishes the stream owner before getReader can re-enter terminal teardown", async () => {
    const read = vi.fn()
    const cancel = vi.fn(async () => {})
    const releaseLock = vi.fn()
    const reader = { read, cancel, releaseLock }
    let deleting: Promise<void> | null = null
    const stream = {
      getReader: vi.fn(() => {
        deleting = deleteChatSession("session")
        return reader
      }),
      cancel: vi.fn(async () => {}),
    }
    vi.mocked(apiFetch).mockResolvedValue({ body: stream } as unknown as Response)
    vi.spyOn(globalThis, "fetch").mockResolvedValue({ ok: true, status: 204 } as Response)

    await expect(sendChatMessageStream("session", "question", vi.fn())).rejects.toMatchObject({
      name: "AbortError",
    })
    await expect(deleting).resolves.toBeUndefined()

    expect(read).not.toHaveBeenCalled()
    expect(cancel).toHaveBeenCalledOnce()
    expect(releaseLock).toHaveBeenCalledOnce()
    expect(stream.cancel).not.toHaveBeenCalled()
  })

  it("rolls back the response body when reader acquisition throws", async () => {
    const setupError = new Error("reader acquisition failed")
    const cancelStream = vi.fn(async () => {})
    vi.mocked(apiFetch).mockResolvedValue({
      body: {
        getReader: vi.fn(() => { throw setupError }),
        cancel: cancelStream,
      },
    } as unknown as Response)

    await expect(sendChatMessageStream("session", "question", vi.fn())).rejects.toBe(setupError)
    expect(cancelStream).toHaveBeenCalledOnce()
  })

  it("joins an active read through terminal session deletion before DELETE", async () => {
    const readResult = deferred<ReadableStreamReadResult<Uint8Array<ArrayBuffer>>>()
    const read = vi.fn(() => readResult.promise)
    const cancel = vi.fn(async () => {
      readResult.resolve({ done: true, value: undefined })
    })
    const releaseLock = vi.fn()
    vi.mocked(apiFetch).mockResolvedValue(responseWithReader({ read, cancel, releaseLock }))
    const deleteFetch = vi.spyOn(globalThis, "fetch")
      .mockResolvedValue({ ok: true, status: 204 } as Response)

    const sending = sendChatMessageStream("session", "question", vi.fn())
    await vi.waitFor(() => expect(read).toHaveBeenCalledOnce())
    const deleting = deleteChatSession("session")

    await expect(deleting).resolves.toBeUndefined()
    await expect(sending).rejects.toMatchObject({ name: "AbortError" })
    expect(cancel).toHaveBeenCalledOnce()
    expect(releaseLock).toHaveBeenCalledOnce()
    expect(deleteFetch).toHaveBeenCalledOnce()
  })

  it("does not retry an abort cleanup failure inside the same send attempt", async () => {
    const readResult = deferred<ReadableStreamReadResult<Uint8Array<ArrayBuffer>>>()
    const read = vi.fn(() => readResult.promise)
    const cleanupError = new Error("reader cancel failed")
    const cancel = vi.fn()
      .mockRejectedValueOnce(cleanupError)
      .mockImplementationOnce(async () => {
        readResult.resolve({ done: true, value: undefined })
      })
    const releaseLock = vi.fn()
    const controller = new AbortController()
    vi.mocked(apiFetch).mockResolvedValue(responseWithReader({ read, cancel, releaseLock }))

    const sending = sendChatMessageStream(
      "session",
      "question",
      vi.fn(),
      controller.signal,
    )
    await vi.waitFor(() => expect(read).toHaveBeenCalledOnce())
    controller.abort(new DOMException("cancelled", "AbortError"))

    await expect(sending).rejects.toBeInstanceOf(AggregateError)
    expect(cancel).toHaveBeenCalledOnce()
    expect(releaseLock).not.toHaveBeenCalled()

    vi.spyOn(globalThis, "fetch").mockResolvedValue({ ok: true, status: 204 } as Response)
    await expect(deleteChatSession("session")).resolves.toBeUndefined()
    expect(cancel).toHaveBeenCalledTimes(2)
    expect(releaseLock).toHaveBeenCalledOnce()
  })


  it("publishes the physical read before reader.read can re-enter terminal deletion", async () => {
    const readResult = deferred<ReadableStreamReadResult<Uint8Array<ArrayBuffer>>>()
    let deleting: Promise<void> | null = null
    const read = vi.fn(() => {
      deleting = deleteChatSession("session")
      return readResult.promise
    })
    const cancel = vi.fn(async () => {
      readResult.resolve({ done: true, value: undefined })
    })
    const releaseLock = vi.fn()
    vi.mocked(apiFetch).mockResolvedValue(responseWithReader({ read, cancel, releaseLock }))
    const deleteFetch = vi.spyOn(globalThis, "fetch")
      .mockResolvedValue({ ok: true, status: 204 } as Response)

    const sending = sendChatMessageStream("session", "question", vi.fn())
    const sendingResult = expect(sending).rejects.toMatchObject({ name: "AbortError" })
    await vi.waitFor(() => expect(deleting).not.toBeNull())

    await expect(deleting).resolves.toBeUndefined()
    await sendingResult
    expect(cancel).toHaveBeenCalledOnce()
    expect(releaseLock).toHaveBeenCalledOnce()
    expect(deleteFetch).toHaveBeenCalledOnce()
  })

  it("rolls back an abort listener that is registered and then throws", async () => {
    const setupError = new Error("listener registration failed after mutation")
    const listeners = new Set<EventListenerOrEventListenerObject>()
    const signal = {
      aborted: false,
      reason: undefined,
      addEventListener(
        type: string,
        listener: EventListenerOrEventListenerObject,
      ) {
        if (type === "abort") listeners.add(listener)
        throw setupError
      },
      removeEventListener(
        type: string,
        listener: EventListenerOrEventListenerObject,
      ) {
        if (type === "abort") listeners.delete(listener)
      },
    } as AbortSignal
    const read = vi.fn()
    const cancel = vi.fn(async () => {})
    const releaseLock = vi.fn()
    vi.mocked(apiFetch).mockResolvedValue(responseWithReader({ read, cancel, releaseLock }))

    await expect(sendChatMessageStream(
      "session",
      "question",
      vi.fn(),
      signal,
    )).rejects.toBe(setupError)

    expect(listeners.size).toBe(0)
    expect(read).not.toHaveBeenCalled()
    expect(cancel).toHaveBeenCalledOnce()
    expect(releaseLock).toHaveBeenCalledOnce()
  })

  it("retains a failed abort-listener removal even when failure reporting throws", async () => {
    const cleanupError = new Error("listener removal failed")
    const reportingError = new Error("telemetry failed")
    const removeEventListener = vi.fn()
      .mockImplementationOnce(() => {
        throw cleanupError
      })
      .mockImplementation(() => {})
    const signal = {
      aborted: false,
      reason: undefined,
      addEventListener: vi.fn(),
      removeEventListener,
    } as unknown as AbortSignal
    const read = vi.fn().mockResolvedValue({ done: true, value: undefined })
    const cancel = vi.fn(async () => {})
    const releaseLock = vi.fn()
    vi.mocked(apiFetch).mockResolvedValue(responseWithReader({ read, cancel, releaseLock }))
    vi.mocked(reportClientEvent).mockImplementation(() => {
      throw reportingError
    })

    const sending = sendChatMessageStream(
      "session",
      "question",
      vi.fn(),
      signal,
    )
    await expect(sending).rejects.toMatchObject({
      message: "Chat stream cleanup debt remains",
      errors: [expect.objectContaining({
        message: "Chat stream reader cleanup failed",
        errors: [expect.objectContaining({ cause: cleanupError })],
      })],
    })
    expect(removeEventListener).toHaveBeenCalledOnce()
    expect(releaseLock).toHaveBeenCalledOnce()

    vi.mocked(reportClientEvent).mockReset()
    vi.spyOn(globalThis, "fetch").mockResolvedValue({ ok: true, status: 204 } as Response)
    await expect(deleteChatSession("session")).resolves.toBeUndefined()
    expect(removeEventListener).toHaveBeenCalledTimes(2)
    expect(cancel).not.toHaveBeenCalled()
  })

})

describe("chat session deletion contract", () => {
  beforeEach(() => {
    vi.mocked(reportClientEvent).mockClear()
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it("throws a typed error for a non-successful DELETE", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue({ ok: false, status: 503 } as Response)

    await expect(deleteChatSession("session-id")).rejects.toMatchObject({
      name: "ChatApiError",
      status: 503,
    })
    expect(reportClientEvent).toHaveBeenCalledWith(
      "chat_session_delete_error",
      expect.stringContaining("503"),
    )
  })

  it("throws instead of treating a network failure as successful cleanup", async () => {
    vi.spyOn(globalThis, "fetch").mockRejectedValue(new Error("offline"))

    await expect(deleteChatSession("session-id")).rejects.toBeInstanceOf(ChatApiError)
    expect(reportClientEvent).toHaveBeenCalledWith("chat_session_delete_error", "offline")
  })
})
