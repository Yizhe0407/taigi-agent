import { watch, type Ref } from "vue"

import type { ConversationState } from "../types"

/** Generic single-shot timer: `start()` (re)arms it, `clear()` disarms it. */
function useFuseTimer(ms: number, onExpire: () => void) {
  let timer: number | null = null

  function clear() {
    if (timer !== null) { clearTimeout(timer); timer = null }
  }

  function start() {
    clear()
    timer = window.setTimeout(() => {
      timer = null
      onExpire()
    }, ms)
  }

  return { start, clear }
}

/**
 * 30s safety fuse: total-failure fallback if neither a reply nor a cancel
 * ever arrives for a turn — force back to listening rather than leaving the
 * UI stuck in `thinking`. Armed manually on each user turn (transcript),
 * cleared by every event that legitimately leaves `thinking`.
 */
export function useThinkingFuse(onExpire: () => void) {
  return useFuseTimer(30_000, onExpire)
}

const PROCESSING_FUSE_MS = 10_000

/**
 * Processing fuse: `user_silent` -> `transcript` has no guaranteed follow-up
 * — backend drops empty ASR results without emitting any event — so
 * "辨識中…" could otherwise hang until the 45s idle path. Armed/cleared
 * automatically by watching the state itself: any transition out of
 * `processing` disarms it.
 */
export function useProcessingFuse(
  state: Readonly<Ref<ConversationState>>,
  onExpire: () => void,
) {
  const fuse = useFuseTimer(PROCESSING_FUSE_MS, onExpire)
  watch(state, (next) => {
    fuse.clear()
    if (next === "processing") fuse.start()
  })
  return { clear: fuse.clear }
}
