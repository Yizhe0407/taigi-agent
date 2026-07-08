/**
 * Best-effort frontend → backend error reporting for the unattended kiosk.
 * POSTs to /api/client-events via sendBeacon (survives page teardown) with a
 * fetch keepalive fallback. Client-side flood guard: same message deduped for
 * 60s, max 10 events/minute — backend also truncates, this just avoids
 * spamming the wire.
 */
import { apiBaseUrl } from "@/lib/api"

const MESSAGE_LIMIT = 500
const DETAIL_LIMIT = 2000
const DEDUPE_MS = 60_000
const RATE_LIMIT_PER_MINUTE = 10

// ponytail: module-level Map is enough for a single-tab kiosk; no cross-tab dedupe.
const lastSentAt = new Map<string, number>()
let windowStart = 0
let countInWindow = 0

export function reportClientEvent(type: string, message: string, detail?: string): void {
  const now = Date.now()

  // Prune stale dedupe entries opportunistically — kiosk runs indefinitely,
  // this keeps the map bounded without a timer.
  for (const [key, t] of lastSentAt) {
    if (now - t > DEDUPE_MS) lastSentAt.delete(key)
  }

  const dedupeKey = `${type}:${message}`
  if (lastSentAt.has(dedupeKey)) return

  if (now - windowStart > 60_000) {
    windowStart = now
    countInWindow = 0
  }
  if (countInWindow >= RATE_LIMIT_PER_MINUTE) return
  countInWindow++
  lastSentAt.set(dedupeKey, now)

  const payload = JSON.stringify({
    type,
    message: message.slice(0, MESSAGE_LIMIT),
    detail: detail ? detail.slice(0, DETAIL_LIMIT) : undefined,
    ts: now,
  })
  const url = `${apiBaseUrl}/api/client-events`

  if (navigator.sendBeacon?.(url, new Blob([payload], { type: "application/json" }))) return

  void fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: payload,
    keepalive: true,
  }).catch(() => {})
}
