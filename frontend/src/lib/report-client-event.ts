/**
 * Best-effort frontend → backend error reporting for the unattended kiosk.
 * POSTs to /api/client-events via sendBeacon (survives page teardown) with a
 * fetch keepalive fallback. Client-side flood guard: same message deduped for
 * 60s, max 10 events/minute — backend also truncates, this just avoids
 * spamming the wire.
 */
import { apiBaseUrl } from "@/lib/api"
import {
  createResourceOwner,
  type ResourceReleaseAttempt,
} from "@/lib/resource-owner"

const MESSAGE_LIMIT = 500
const DETAIL_LIMIT = 2000
const DEDUPE_MS = 60_000
const RATE_LIMIT_PER_MINUTE = 10
const MAX_ACTIVE_FETCH_DELIVERIES = RATE_LIMIT_PER_MINUTE

// Module-level state is the document-lifespan authority for one kiosk tab.
// Every fallback shares its permanent close signal, so there is no
// per-delivery controller construction gap or parallel abort-debt registry.
const deliveryOwner = createResourceOwner("client event reporting")
const lastSentAt = new Map<string, number>()
const activeFetchDeliveries = new Set<FetchDelivery>()
let windowStart = 0
let countInWindow = 0
let shutdownOperation: Promise<void> | null = null

interface FetchDelivery {
  promise: Promise<void>
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

async function deliverWithFetch(
  url: string,
  payload: string,
  delivery: FetchDelivery,
): Promise<void> {
  try {
    deliveryOwner.signal.throwIfAborted()
    const response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: payload,
      keepalive: true,
      signal: deliveryOwner.signal,
    })
    if (!response.ok) {
      throw new Error(`POST /api/client-events -> ${response.status}`)
    }
  } catch (error) {
    // Owner-requested cancellation is the successful terminal state of this
    // best-effort fallback. Every other failure is observable, but must never
    // recurse through reportClientEvent().
    if (!deliveryOwner.disposed) {
      console.error("Client event delivery failed:", errorMessage(error))
    }
  } finally {
    activeFetchDeliveries.delete(delivery)
  }
}

function startFetchDelivery(url: string, payload: string): void {
  // sendBeacon implementations are user-agent code and can re-enter shutdown;
  // acquire again at the actual fallback boundary.
  if (deliveryOwner.disposed) return
  if (activeFetchDeliveries.size >= MAX_ACTIVE_FETCH_DELIVERIES) {
    console.error(
      "Client event delivery skipped:",
      `already ${MAX_ACTIVE_FETCH_DELIVERIES} fallback requests in flight`,
    )
    return
  }

  let releaseStart!: () => void
  const startGate = new Promise<void>((resolve) => {
    releaseStart = resolve
  })
  let delivery!: FetchDelivery
  const promise = startGate.then(() => deliverWithFetch(url, payload, delivery))
  delivery = { promise }

  // Publish the terminal operation before releasing the microtask gate that
  // may enter fetch/user-agent code. Shutdown can therefore abort and join the
  // exact operation even when fetch synchronously re-enters it.
  activeFetchDeliveries.add(delivery)
  releaseStart()
}

export function shutdownClientEventReporting(
  attempt: ResourceReleaseAttempt = {},
): Promise<void> {
  lastSentAt.clear()
  if (shutdownOperation) return shutdownOperation
  if (
    deliveryOwner.disposed
    && deliveryOwner.settled
    && activeFetchDeliveries.size === 0
  ) {
    return Promise.resolve()
  }

  const deliveries = [...activeFetchDeliveries]
  let cleanupFailed = false
  let cleanupFailure: unknown
  let releaseCleanup!: () => void
  const cleanupStarted = new Promise<void>((resolve) => {
    releaseCleanup = resolve
  })

  // Publish the joinable teardown operation before entering AbortController.
  // Re-entrant shutdown calls therefore join this exact physical attempt.
  let operation!: Promise<void>
  operation = cleanupStarted
    .then(async () => {
      // If abort physically closed the shared signal, every captured fetch is
      // guaranteed to become terminal and must be joined. If abort failed
      // before mutation, do not wait forever; retain controller debt in the
      // owner and return the cleanup failure so a distinct attempt can retry.
      if (deliveryOwner.signal.aborted) {
        await Promise.all(deliveries.map(delivery => delivery.promise))
      }

      if (cleanupFailed) throw cleanupFailure
      if (!deliveryOwner.settled) {
        throw new Error("Client event reporting cleanup remains unresolved")
      }
    })
    .finally(() => {
      if (shutdownOperation === operation) shutdownOperation = null
    })
  shutdownOperation = operation

  try {
    deliveryOwner.dispose(attempt)
  } catch (error) {
    cleanupFailed = true
    cleanupFailure = error
  } finally {
    releaseCleanup()
  }

  return operation
}

export function reportClientEvent(type: string, message: string, detail?: string): void {
  if (deliveryOwner.disposed) return

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

  try {
    if (navigator.sendBeacon?.(url, new Blob([payload], { type: "application/json" }))) return
  } catch {
    // A throwing beacon implementation has transferred no ownership; the
    // fetch owner below becomes the sole delivery authority.
  }

  startFetchDelivery(url, payload)
}
