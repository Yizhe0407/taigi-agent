import { apiBaseUrl, apiFetch, ApiError } from "@/lib/api"

export class ChatApiError extends ApiError {
  constructor(message: string, status: number | null = null) {
    super(message, status)
    this.name = "ChatApiError"
  }
}

const AGENT_NETWORK_MESSAGE = "無法連線到助理服務"

/** Create a new agent chat session. Returns the opaque session ID. */
export async function createChatSession(): Promise<string> {
  const response = await apiFetch("/api/chat/sessions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    errorClass: ChatApiError,
    networkMessage: AGENT_NETWORK_MESSAGE,
  })
  const body = (await response.json()) as { sessionId: string }
  return body.sessionId
}

/** Send a user message and receive the agent reply. */
export async function sendChatMessage(
  sessionId: string,
  message: string,
  signal?: AbortSignal,
): Promise<string> {
  const response = await apiFetch(
    `/api/chat/sessions/${sessionId}/messages`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message }),
      signal,
      errorClass: ChatApiError,
      networkMessage: AGENT_NETWORK_MESSAGE,
    },
  )
  const body = (await response.json()) as { reply: string }
  return body.reply
}

/** POST audio blob to backend ASR proxy. Returns transcription text. */
export async function transcribeAudio(audio: Blob): Promise<string> {
  const ext = audio.type.includes("wav") ? "wav" : audio.type.includes("ogg") ? "ogg" : "webm"
  const form = new FormData()
  form.append("file", audio, `audio.${ext}`)

  const response = await apiFetch("/api/asr", {
    method: "POST",
    body: form,
    errorClass: ChatApiError,
    networkMessage: "無法連線到 ASR 服務",
  })
  const body = (await response.json()) as { text: string }
  return body.text
}

/** POST text to /api/tts; returns audio Blob (audio/wav or audio/mpeg). */
export async function synthesizeSpeech(text: string, signal?: AbortSignal): Promise<Blob> {
  const response = await apiFetch("/api/tts", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text }),
    signal,
    errorClass: ChatApiError,
    networkMessage: "無法連線到 TTS 服務",
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
