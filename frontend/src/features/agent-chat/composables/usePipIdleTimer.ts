import { ref } from "vue"

// 45s of no activity -> warning card w/ 15s countdown -> zero closes. Any
// voice event or touch (see markActivity) cancels and returns to the prior
// state. Replaces the old blind 60s auto-close.
const IDLE_WARN_AFTER_MS = 45_000
const IDLE_WARN_COUNTDOWN_S = 15

/**
 * PiP idle-close timer: 45s of no activity shows a 15s-countdown warning
 * card, then `onExpire` (closes the PiP). Any caller-observed activity calls
 * `markActivity`/`resetIdleTimer` to push the clock back out.
 */
export function usePipIdleTimer(onExpire: () => void) {
  const showIdleWarning = ref(false)
  const idleWarnSecondsLeft = ref(IDLE_WARN_COUNTDOWN_S)
  let idleTimer: number | null = null
  let idleWarnInterval: number | null = null

  function clearIdleTimer() {
    if (idleTimer !== null) { clearTimeout(idleTimer); idleTimer = null }
  }

  function clearIdleWarnInterval() {
    if (idleWarnInterval !== null) { clearInterval(idleWarnInterval); idleWarnInterval = null }
  }

  function dismissIdleWarning() {
    showIdleWarning.value = false
    clearIdleWarnInterval()
  }

  function startIdleWarning() {
    showIdleWarning.value = true
    idleWarnSecondsLeft.value = IDLE_WARN_COUNTDOWN_S
    clearIdleWarnInterval()
    idleWarnInterval = window.setInterval(() => {
      idleWarnSecondsLeft.value -= 1
      if (idleWarnSecondsLeft.value <= 0) {
        clearIdleWarnInterval()
        onExpire()
      }
    }, 1000)
  }

  function resetIdleTimer() {
    clearIdleTimer()
    dismissIdleWarning()
    idleTimer = window.setTimeout(startIdleWarning, IDLE_WARN_AFTER_MS)
  }

  /** Any voice event or touch resets the 45s idle clock and cancels the warning card. */
  function markActivity() {
    resetIdleTimer()
  }

  return {
    showIdleWarning,
    idleWarnSecondsLeft,
    resetIdleTimer,
    clearIdleTimer,
    clearIdleWarnInterval,
    dismissIdleWarning,
    markActivity,
  }
}
