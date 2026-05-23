import { apiBaseUrl, ApiError, parseErrorBody } from "@/lib/api"

export class ChatApiError extends ApiError {
  constructor(message: string, status: number | null = null) {
    super(message, status)
    this.name = "ChatApiError"
  }
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
    throw new ChatApiError(await parseErrorBody(response), response.status)
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
    throw new ChatApiError(await parseErrorBody(response), response.status)
  }

  const body = (await response.json()) as { reply: string }
  return body.reply
}

/** POST audio blob to backend ASR proxy. Returns transcription text. */
export async function transcribeAudio(audio: Blob): Promise<string> {
  const ext = audio.type.includes("wav") ? "wav" : audio.type.includes("ogg") ? "ogg" : "webm"
  const form = new FormData()
  form.append("file", audio, `audio.${ext}`)

  let response: Response
  try {
    response = await fetch(`${apiBaseUrl}/api/asr`, {
      method: "POST",
      body: form,
    })
  } catch {
    throw new ChatApiError("無法連線到 ASR 服務")
  }

  if (!response.ok) {
    throw new ChatApiError(await parseErrorBody(response), response.status)
  }

  const body = (await response.json()) as { text: string }
  return body.text
}

/** POST text to /api/tts; returns audio Blob (audio/wav or audio/mpeg). */
export async function synthesizeSpeech(text: string, signal?: AbortSignal): Promise<Blob> {
  let response: Response
  try {
    response = await fetch(`${apiBaseUrl}/api/tts`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
      signal,
    })
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error
    throw new ChatApiError("無法連線到 TTS 服務")
  }

  if (!response.ok) {
    throw new ChatApiError(await parseErrorBody(response), response.status)
  }

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
