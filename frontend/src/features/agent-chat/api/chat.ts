import { API_NETWORK_MESSAGES } from "@/lib/api-messages"
import {
  apiBaseUrl,
  apiFetch,
  ApiError,
  readBlobResponse,
  readJsonResponse,
} from "@/lib/api"
import { reportClientEvent } from "@/lib/report-client-event"

export class ChatApiError extends ApiError {
  constructor(message: string, status: number | null = null) {
    super(message, status)
    this.name = "ChatApiError"
  }
}

type PendingStreamRead = {
  terminal: Promise<void>
  rejectForClosing(reason: unknown): void
}

type StreamCleanupAttempt = object

type StreamCleanupAttemptState = {
  operation: Promise<void>
  failure: unknown | typeof NO_CLEANUP_FAILURE
}

type ChatStreamOwner = {
  sessionId: string
  stream: ReadableStream<Uint8Array>
  reader: ReadableStreamDefaultReader<Uint8Array> | null
  readerAcquisitionPending: boolean
  reachedEof: boolean
  cancellationComplete: boolean
  readerLockReleased: boolean
  abortSignal: AbortSignal | null
  abortListener: (() => void) | null
  abortListenerOwned: boolean
  pendingRead: PendingStreamRead | null
  closing: boolean
  closingReason: unknown
  cleanupTail: Promise<void> | null
  cleanupAttempts: WeakMap<StreamCleanupAttempt, StreamCleanupAttemptState>
}

const streamOwnersBySession = new Map<string, Set<ChatStreamOwner>>()
const NO_STREAM_ERROR = Symbol("no stream error")
const NO_CLEANUP_FAILURE = Symbol("no cleanup failure")

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

function reportCleanupFailure(type: string, error: unknown) {
  try {
    reportClientEvent(type, errorMessage(error))
  } catch {
    // Reporting is best-effort only. The authoritative cleanup error remains
    // retained by its owner and is thrown through the teardown contract.
  }
}

function cleanupFailure(resource: string, error: unknown): Error {
  return new Error(`${resource}: ${errorMessage(error)}`, { cause: error })
}

function registerStreamOwner(owner: ChatStreamOwner) {
  let owners = streamOwnersBySession.get(owner.sessionId)
  if (!owners) {
    owners = new Set()
    streamOwnersBySession.set(owner.sessionId, owners)
  }
  owners.add(owner)
}

function unregisterStreamOwner(owner: ChatStreamOwner) {
  const owners = streamOwnersBySession.get(owner.sessionId)
  if (!owners) return
  owners.delete(owner)
  if (owners.size === 0) streamOwnersBySession.delete(owner.sessionId)
}

function streamClosedError(): DOMException {
  return new DOMException("Chat stream was closed", "AbortError")
}

function markStreamClosing(owner: ChatStreamOwner, reason: unknown = streamClosedError()) {
  if (owner.closing) return
  owner.closing = true
  owner.closingReason = reason
  owner.pendingRead?.rejectForClosing(reason)
}

function streamOwnerSettled(owner: ChatStreamOwner): boolean {
  return !owner.readerAcquisitionPending
    && (owner.reachedEof || owner.cancellationComplete)
    && (owner.reader === null || owner.readerLockReleased)
    && !owner.abortListenerOwned
    && owner.pendingRead === null
}

async function runStreamCleanup(owner: ChatStreamOwner): Promise<void> {
  const failures: Error[] = []

  if (owner.reachedEof) owner.cancellationComplete = true
  if (!owner.cancellationComplete) {
    try {
      if (owner.reader && !owner.readerLockReleased) {
        await owner.reader.cancel()
      } else {
        await owner.stream.cancel()
      }
      owner.cancellationComplete = true
    } catch (error) {
      failures.push(cleanupFailure("stream cancellation", error))
    }
  }

  if (owner.cancellationComplete && owner.pendingRead) {
    await owner.pendingRead.terminal
  }

  if (owner.abortListenerOwned && owner.abortSignal && owner.abortListener) {
    try {
      owner.abortSignal.removeEventListener("abort", owner.abortListener)
      owner.abortListenerOwned = false
    } catch (error) {
      failures.push(cleanupFailure("stream abort listener", error))
    }
  }

  if (owner.reader && !owner.readerLockReleased && owner.pendingRead === null) {
    try {
      owner.reader.releaseLock()
      owner.readerLockReleased = true
    } catch (error) {
      failures.push(cleanupFailure("reader lock", error))
    }
  }

  if (streamOwnerSettled(owner)) unregisterStreamOwner(owner)
  if (failures.length > 0) {
    throw new AggregateError(failures, "Chat stream reader cleanup failed")
  }
  if (!streamOwnerSettled(owner)) {
    throw new Error("Chat stream cleanup left unresolved resources")
  }
}

function startStreamCleanup(
  owner: ChatStreamOwner,
  attempt: StreamCleanupAttempt,
): Promise<void> {
  markStreamClosing(owner)
  const existingAttempt = owner.cleanupAttempts.get(attempt)
  if (existingAttempt) return existingAttempt.operation

  const state: StreamCleanupAttemptState = {
    operation: Promise.resolve(),
    failure: NO_CLEANUP_FAILURE,
  }
  const predecessor = owner.cleanupTail
  const physicalOperation = predecessor
    ? predecessor.then(() => runStreamCleanup(owner))
    : Promise.resolve().then(() => runStreamCleanup(owner))

  let operation!: Promise<void>
  operation = physicalOperation
    .then(undefined, (error) => {
      state.failure = error
      reportCleanupFailure("chat_stream_cleanup_error", error)
    })
    .then(() => {
      if (owner.cleanupTail === operation) owner.cleanupTail = null
    })
  state.operation = operation
  owner.cleanupAttempts.set(attempt, state)
  owner.cleanupTail = operation
  return operation
}

async function settleStreamOwner(
  owner: ChatStreamOwner,
  attempt: StreamCleanupAttempt,
): Promise<void> {
  await startStreamCleanup(owner, attempt)
  const state = owner.cleanupAttempts.get(attempt)
  if (!state) throw new Error("Chat stream cleanup attempt was not published")
  if (state.failure !== NO_CLEANUP_FAILURE) {
    throw new AggregateError([state.failure], "Chat stream cleanup debt remains")
  }
  if (!streamOwnerSettled(owner)) {
    throw new Error("Chat stream cleanup debt remains unresolved")
  }
}

async function settleSessionStreamCleanup(
  sessionId: string,
  attempt: StreamCleanupAttempt,
): Promise<void> {
  const owners = [...(streamOwnersBySession.get(sessionId) ?? [])]
  if (owners.length === 0) return

  const results = await Promise.allSettled(
    owners.map(owner => settleStreamOwner(owner, attempt)),
  )
  const failures = results.flatMap(result =>
    result.status === "rejected" ? [result.reason] : [],
  )
  if (failures.length > 0) {
    const error = new AggregateError(failures, "Chat session stream cleanup debt remains")
    reportCleanupFailure("chat_stream_cleanup_error", error)
    throw error
  }
}

function createStreamOwner(
  sessionId: string,
  stream: ReadableStream<Uint8Array>,
): ChatStreamOwner {
  const owner: ChatStreamOwner = {
    sessionId,
    stream,
    reader: null,
    readerAcquisitionPending: true,
    reachedEof: false,
    cancellationComplete: false,
    readerLockReleased: false,
    abortSignal: null,
    abortListener: null,
    abortListenerOwned: false,
    pendingRead: null,
    closing: false,
    closingReason: streamClosedError(),
    cleanupTail: null,
    cleanupAttempts: new WeakMap(),
  }
  registerStreamOwner(owner)
  return owner
}

function installStreamAbort(
  owner: ChatStreamOwner,
  attempt: StreamCleanupAttempt,
  signal?: AbortSignal,
) {
  if (!signal) return

  const onAbort = () => {
    let reason: unknown
    try {
      reason = signal.reason
    } catch (error) {
      reason = error
    }
    markStreamClosing(owner, reason)
    startStreamCleanup(owner, attempt)
  }
  owner.abortSignal = signal
  owner.abortListener = onAbort
  owner.abortListenerOwned = true
  try {
    signal.addEventListener("abort", onAbort)
  } catch (error) {
    throw error
  }
  if (signal.aborted) onAbort()
}

function readStreamChunk(
  owner: ChatStreamOwner,
): Promise<ReadableStreamReadResult<Uint8Array>> {
  const reader = owner.reader
  if (!reader) return Promise.reject(new ChatApiError(API_NETWORK_MESSAGES.agent))
  if (owner.closing) return Promise.reject(owner.closingReason)

  let resolvePublic!: (result: ReadableStreamReadResult<Uint8Array>) => void
  let rejectPublic!: (reason?: unknown) => void
  let publicSettled = false
  const publicRead = new Promise<ReadableStreamReadResult<Uint8Array>>(
    (resolve, reject) => {
      resolvePublic = resolve
      rejectPublic = reject
    },
  )

  let releaseStart!: () => void
  const startGate = new Promise<void>((resolve) => {
    releaseStart = resolve
  })
  const physicalRead = startGate.then(() => reader.read())
  let pendingRead!: PendingStreamRead
  const terminal = physicalRead.then(
    (result) => {
      if (owner.pendingRead === pendingRead) owner.pendingRead = null
      if (publicSettled) return
      publicSettled = true
      resolvePublic(result)
    },
    (error) => {
      if (owner.pendingRead === pendingRead) owner.pendingRead = null
      if (publicSettled) return
      publicSettled = true
      rejectPublic(error)
    },
  )
  pendingRead = {
    terminal,
    rejectForClosing(reason) {
      if (publicSettled) return
      publicSettled = true
      rejectPublic(reason)
    },
  }

  // Publish both the externally visible read and its always-fulfilled terminal
  // observer before entering reader.read(). Teardown can reject the caller
  // immediately, cancel the reader, and still join the exact physical read.
  owner.pendingRead = pendingRead
  releaseStart()
  if (owner.closing) pendingRead.rejectForClosing(owner.closingReason)
  return publicRead
}

/** Idempotently create the client-owned chat session ID. */
export async function createChatSession(
  sessionId: string,
  signal?: AbortSignal,
): Promise<void> {
  const response = await apiFetch(`/api/chat/sessions/${sessionId}`, {
    method: "PUT",
    errorClass: ChatApiError,
    networkMessage: API_NETWORK_MESSAGES.agent,
    signal,
  })
  const body = await readJsonResponse<{ sessionId?: string }>(response, signal)
  if (body.sessionId !== sessionId) {
    throw new ChatApiError("對話階段識別碼不一致")
  }
}

/**
 * Send a user message and stream the reply via SSE.
 *
 * `onDelta` fires per text chunk; resolves with the full reply
 * (concatenation of all deltas). HTTP, protocol, callback, and reader cleanup
 * failures reject. The session owns active readers and every failed exact
 * cleanup action until the next stream or terminal DELETE retries it.
 */
export async function sendChatMessageStream(
  sessionId: string,
  message: string,
  onDelta: (delta: string) => void,
  signal?: AbortSignal,
): Promise<string> {
  const cleanupAttempt: StreamCleanupAttempt = {}
  await settleSessionStreamCleanup(sessionId, cleanupAttempt)

  const response = await apiFetch(
    `/api/chat/sessions/${sessionId}/messages/stream`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message }),
      signal,
      errorClass: ChatApiError,
      networkMessage: API_NETWORK_MESSAGES.agent,
    },
  )
  const stream = response.body
  if (!stream) throw new ChatApiError(API_NETWORK_MESSAGES.agent)

  const owner = createStreamOwner(sessionId, stream)
  let reply = ""
  let primaryError: unknown | typeof NO_STREAM_ERROR = NO_STREAM_ERROR

  try {
    try {
      owner.reader = stream.getReader()
    } finally {
      owner.readerAcquisitionPending = false
    }
    if (owner.closing) throw owner.closingReason
    installStreamAbort(owner, cleanupAttempt, signal)
    if (owner.closing) throw owner.closingReason

    const decoder = new TextDecoder()
    let buffer = ""

    streamLoop: for (;;) {
      const { done, value } = await readStreamChunk(owner)
      if (done) {
        owner.reachedEof = true
        break
      }
      buffer += decoder.decode(value, { stream: true })
      let boundary: number
      while ((boundary = buffer.indexOf("\n\n")) >= 0) {
        const rawEvent = buffer.slice(0, boundary)
        buffer = buffer.slice(boundary + 2)
        const dataLine = rawEvent.split("\n").find(line => line.startsWith("data: "))
        if (!dataLine) continue
        const payload = JSON.parse(dataLine.slice(6)) as {
          delta?: string
          done?: boolean
          error?: string
        }
        if (payload.error) throw new ChatApiError(payload.error)
        if (payload.delta) {
          reply += payload.delta
          onDelta(payload.delta)
        }
        if (payload.done) break streamLoop
      }
    }
  } catch (error) {
    primaryError = error
  }

  let cleanupError: unknown | typeof NO_STREAM_ERROR = NO_STREAM_ERROR
  try {
    await settleStreamOwner(owner, cleanupAttempt)
  } catch (error) {
    cleanupError = error
  }

  if (primaryError !== NO_STREAM_ERROR) {
    if (cleanupError !== NO_STREAM_ERROR) {
      throw new AggregateError(
        [primaryError, cleanupError],
        "Chat stream failed and reader cleanup remains incomplete",
      )
    }
    throw primaryError
  }
  if (cleanupError !== NO_STREAM_ERROR) throw cleanupError
  return reply
}

/** POST text to /api/tts; returns audio Blob (audio/wav or audio/mpeg). */
export async function synthesizeSpeech(text: string, signal?: AbortSignal): Promise<Blob> {
  const response = await apiFetch("/api/tts", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text }),
    signal,
    errorClass: ChatApiError,
    networkMessage: API_NETWORK_MESSAGES.tts,
  })
  return readBlobResponse(response, signal)
}

/**
 * Terminal session teardown. Active stream readers and retained stream cleanup
 * debt are settled before the single unload-safe DELETE is allowed to run.
 */
export async function deleteChatSession(sessionId: string): Promise<void> {
  await settleSessionStreamCleanup(sessionId, {})

  let response: Response
  try {
    response = await fetch(`${apiBaseUrl}/api/chat/sessions/${sessionId}`, {
      method: "DELETE",
      keepalive: true,
    })
  } catch (cause) {
    const error = new ChatApiError(API_NETWORK_MESSAGES.agent)
    reportCleanupFailure("chat_session_delete_error", cause)
    throw error
  }

  if (!response.ok) {
    const error = new ChatApiError(
      `DELETE /api/chat/sessions/${sessionId} -> ${response.status}`,
      response.status,
    )
    reportCleanupFailure("chat_session_delete_error", error)
    throw error
  }
}
