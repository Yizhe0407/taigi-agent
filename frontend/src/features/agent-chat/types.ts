export type PipCorner = "tl" | "tr" | "bl" | "br"
export type WebRTCState = "disconnected" | "connecting" | "connected" | "error"
export type PipSize = "sm" | "md" | "lg"

/** PiP voice conversation phase — single source of truth, see useConversationState. */
export type ConversationState =
  | "connecting"
  | "listening"
  | "userSpeaking"
  | "processing"
  | "thinking"
  | "speaking"

export const PIP_SIZES: Record<PipSize, { w: number; h: number }> = {
  sm: { w: 220, h: 275 },
  md: { w: 280, h: 350 },
  lg: { w: 340, h: 425 },
}

export type PipChatMessage = {
  id: string
  role: "user" | "assistant"
  text: string
}
