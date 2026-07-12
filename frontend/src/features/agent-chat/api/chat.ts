import { API_NETWORK_MESSAGES } from "@/lib/api-messages"
import { apiBaseUrl, apiFetch, ApiError } from "@/lib/api"

export class ChatApiError extends ApiError {
  constructor(message: string, status: number | null = null) {
    super(message, status)
    this.name = "ChatApiError"
  }
}

/** Create a new agent chat session. Returns the opaque session ID. */
export async function createChatSession(): Promise<string> {
  const response = await apiFetch("/api/chat/sessions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    errorClass: ChatApiError,
    networkMessage: API_NETWORK_MESSAGES.agent,
  })
  const body = (await response.json()) as { sessionId: string }
  return body.sessionId
}

/**
 * Send a user message and stream the reply via SSE.
 *
 * `onDelta` fires per text chunk; resolves with the full reply
 * (concatenation of all deltas). Falls back to throwing ChatApiError on
 * HTTP errors or in-stream `{"error": ...}` events.
 */
export async function sendChatMessageStream(
  sessionId: string,
  message: string,
  onDelta: (delta: string) => void,
  signal?: AbortSignal,
): Promise<string> {
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
  const reader = response.body?.getReader()
  if (!reader) throw new ChatApiError(API_NETWORK_MESSAGES.agent)

  const decoder = new TextDecoder()
  let buffer = ""
  let reply = ""
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    let boundary
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
      if (payload.done) return reply
    }
  }
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
  return response.blob()
}

/** Explicitly end a session (best-effort, ignore failures). */
export async function deleteChatSession(sessionId: string): Promise<void> {
  try {
    await fetch(`${apiBaseUrl}/api/chat/sessions/${sessionId}`, {
      method: "DELETE",
    })
  } catch {
    // ignore — TTL will clean up
  }
}

/**
 * Fire a session DELETE that survives page unload (`pagehide`/tab kill).
 *
 * `navigator.sendBeacon` can only issue POST, but the endpoint is DELETE and
 * already idempotent, so we use `keepalive` fetch instead — it keeps the
 * request in flight past document teardown without needing a POST-shaped alias
 * on the backend. Best-effort: if it never lands, the server TTL reaps the row.
 */
export function deleteChatSessionBeacon(sessionId: string): void {
  try {
    void fetch(`${apiBaseUrl}/api/chat/sessions/${sessionId}`, {
      method: "DELETE",
      keepalive: true,
    }).catch(() => {})
  } catch {
    // ignore — TTL will clean up
  }
}
