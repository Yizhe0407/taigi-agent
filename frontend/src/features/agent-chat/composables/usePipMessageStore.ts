import { computed, ref, type ComputedRef } from "vue"

import type { PipChatMessage } from "../types"

export const PIP_CHAT_MESSAGE_LIMIT = 100

export type PipMessageReader = ComputedRef<readonly Readonly<PipChatMessage>[]>

export type PipMessageWriter = {
  append(message: PipChatMessage): void
  remove(id: string): boolean
  appendText(id: string, text: string): boolean
  setText(id: string, text: string): boolean
  has(id: string): boolean
  clear(): void
}

/**
 * Sole owner of PiP message history.
 *
 * Producers receive mutation methods instead of the backing array, so every
 * text and voice path is forced through the same bounded invariant. Updates
 * replace message objects rather than leaking mutable references to callers.
 */
export function usePipMessageStore(limit = PIP_CHAT_MESSAGE_LIMIT): {
  messages: PipMessageReader
  writer: PipMessageWriter
} {
  const storedMessages = ref<PipChatMessage[]>([])
  const messages = computed<readonly Readonly<PipChatMessage>[]>(() => storedMessages.value)

  function append(message: PipChatMessage) {
    storedMessages.value = [...storedMessages.value, { ...message }].slice(-limit)
  }

  function updateText(id: string, update: (text: string) => string): boolean {
    const index = storedMessages.value.findIndex(message => message.id === id)
    if (index < 0) return false

    const existing = storedMessages.value[index]
    if (!existing) return false
    const replacement = { ...existing, text: update(existing.text) }
    storedMessages.value = storedMessages.value.map((message, currentIndex) =>
      currentIndex === index ? replacement : message,
    )
    return true
  }

  function remove(id: string): boolean {
    const next = storedMessages.value.filter(message => message.id !== id)
    if (next.length === storedMessages.value.length) return false
    storedMessages.value = next
    return true
  }

  function appendText(id: string, text: string) {
    return updateText(id, existing => existing + text)
  }

  function setText(id: string, text: string) {
    return updateText(id, () => text)
  }

  function has(id: string) {
    return storedMessages.value.some(message => message.id === id)
  }

  function clear() {
    storedMessages.value = []
  }

  return {
    messages,
    writer: { append, remove, appendText, setText, has, clear },
  }
}
