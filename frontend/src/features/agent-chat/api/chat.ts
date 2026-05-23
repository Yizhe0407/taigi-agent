const configuredApiBase = import.meta.env.VITE_API_BASE_URL?.replace(/\/$/, "")
const apiBaseUrl = configuredApiBase ?? ""

export class ChatApiError extends Error {
  readonly status: number | null

  constructor(message: string, status: number | null = null) {
    super(message)
    this.name = "ChatApiError"
    this.status = status
  }
}

type ApiFailure = { detail?: string }

async function errorMessage(response: Response): Promise<string> {
  try {
    const body = (await response.json()) as ApiFailure
    if (body.detail) return body.detail
  } catch {
    // fall through to status-based message
  }
  return `API 回應 ${response.status}`
}

/** Create a new agent chat session. Returns the opaque session ID. */
export async function createChatSession(): Promise<string> {
  let response: Response
  try {
    response = await fetch(`${apiBaseUrl}/api/chat/sessions`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
    })
  } catch {
    throw new ChatApiError("無法連線到助理服務")
  }

  if (!response.ok) {
    throw new ChatApiError(await errorMessage(response), response.status)
  }

  const body = (await response.json()) as { sessionId: string }
  return body.sessionId
}

/** Send a user message and receive the agent reply. */
export async function sendChatMessage(
  sessionId: string,
  message: string,
  signal?: AbortSignal,
): Promise<string> {
  let response: Response
  try {
    response = await fetch(
      `${apiBaseUrl}/api/chat/sessions/${sessionId}/messages`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message }),
        signal,
      },
    )
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error
    throw new ChatApiError("無法連線到助理服務")
  }

  if (!response.ok) {
    throw new ChatApiError(await errorMessage(response), response.status)
  }

  const body = (await response.json()) as { reply: string }
  return body.reply
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
