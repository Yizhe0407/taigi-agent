import { effectScope, ref } from "vue"
import { afterEach, describe, expect, it, vi } from "vitest"

import {
  PIP_CHAT_MESSAGE_LIMIT,
  usePipMessageStore,
} from "@/features/agent-chat/composables/usePipMessageStore"
import { usePipVoiceReveal } from "@/features/agent-chat/composables/usePipVoiceReveal"

describe("PiP message ownership", () => {
  afterEach(() => {
    vi.useRealTimers()
  })

  it("applies the same bounded invariant to every voice message producer", () => {
    const scope = effectScope()
    const state = scope.run(() => {
      const store = usePipMessageStore()
      const reveal = usePipVoiceReveal({
        messages: store.writer,
        displayedAgentText: ref(""),
        onWatchdogExpire: vi.fn(),
      })
      return { store, reveal }
    })
    expect(state).toBeDefined()

    for (let turn = 1; turn <= 51; turn++) {
      state!.reveal.beginVoiceTurn(`user-${turn}`)
      state!.reveal.markAudioStarted()
      state!.reveal.receiveReply(`assistant-${turn}`)
      expect(state!.reveal.finishVoiceReply()).toBe(true)
    }

    expect(state!.store.messages.value).toHaveLength(PIP_CHAT_MESSAGE_LIMIT)
    expect(state!.store.messages.value[0]).toEqual({
      id: "voice-user-2",
      role: "user",
      text: "user-2",
    })
    expect(state!.store.messages.value.at(-1)).toEqual({
      id: "voice-reply-51",
      role: "assistant",
      text: "assistant-51",
    })

    scope.stop()
  })

  it("keeps one reveal delay owner when a subtitle segment is superseded", async () => {
    vi.useFakeTimers()
    const scope = effectScope()
    const state = scope.run(() => {
      const store = usePipMessageStore()
      const reveal = usePipVoiceReveal({
        messages: store.writer,
        displayedAgentText: ref(""),
        onWatchdogExpire: vi.fn(),
      })
      return { store, reveal }
    })
    expect(state).toBeDefined()

    state!.reveal.beginVoiceSession()
    state!.reveal.revealSegment("abc", 300)
    expect(vi.getTimerCount()).toBe(1)

    state!.reveal.revealSegment("xy", 200)
    await Promise.resolve()
    expect(vi.getTimerCount()).toBe(1)

    state!.reveal.cancelVoiceReply()
    expect(vi.getTimerCount()).toBe(0)
    scope.stop()
    expect(vi.getTimerCount()).toBe(0)
  })
})
