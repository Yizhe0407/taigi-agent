import { type Ref } from "vue"

import {
  currentSynchronousScopeReleaseAttempt,
  observeSynchronousScopeTeardown,
  runWithSynchronousScopeReleaseAttempt,
} from "@/lib/component-lifecycle"
import {
  createResourceOwner,
  ResourceOwnerClosedError,
  type AcquiredResource,
  type ResourceOwner,
  type ResourceReleaseAttempt,
} from "@/lib/resource-owner"
import { createOwnedTimeout, type TimerLease } from "@/lib/timer-owner"

import type { PipMessageWriter } from "./usePipMessageStore"

const TTS_WATCHDOG_MS = 4000

type RevealOwnership = {
  text: string
  index: number
  delay: number
  active: boolean
  generation: number
  timerGeneration: number
  timer: TimerLease | null
}

type ReleasedReveal = {
  ownership: RevealOwnership
  wasActive: boolean
  cleanupFailures: unknown[]
}

type VoiceTurnPhase =
  | "opening"
  | "awaiting"
  | "playing"
  | "publishing"
  | "inactive"
  | "settled"

type MessagePublication = {
  readonly owner: ResourceOwner
  state: "acquiring" | "rollback"
}

type TurnClosure = {
  readonly nextPhase: "inactive" | "settled"
  readonly flushParkedReply: boolean
  running: boolean
  lastAttempt: ResourceReleaseAttempt | null
  failed: boolean
  lastFailure: unknown
  parkedReplySettled: boolean
}

type VoiceTurn = {
  readonly id: number
  readonly timerOwner: ResourceOwner
  readonly publications: Set<MessagePublication>
  phase: VoiceTurnPhase
  replyBubbleId: string | null
  parkedReply: string | null
  reveal: RevealOwnership | null
  revealGeneration: number
  ttsWatchdog: TimerLease | null
  ttsWatchdogGeneration: number
  closure: TurnClosure | null
}

type PendingMessagePublication = {
  readonly entry: MessagePublication
  readonly lease: AcquiredResource<void>
}

function throwFailures(failures: unknown[], message: string): void {
  if (failures.length === 1) throw failures[0]
  if (failures.length > 1) throw new AggregateError(failures, message)
}

/**
 * Single lifecycle owner for one assistant voice turn and its bubble.
 *
 * Every turn publishes its own timer owner before any host acquisition. A
 * terminal transition publishes one joinable closure before cleanup, gates all
 * callbacks, and keeps the exact turn reachable until every failed timer or
 * provisional-message rollback succeeds. A successor therefore cannot exist
 * beside unresolved predecessor debt, and synchronous host re-entry cannot
 * mutate a later generation.
 */
export function usePipVoiceReveal(opts: {
  messages: PipMessageWriter
  displayedAgentText: Ref<string>
  /** TTS proven dead: parked reply has just been flushed onto the bubble. */
  onWatchdogExpire: () => void
}) {
  const lifecycleOwner = createResourceOwner("PiP voice reveal lifecycle")
  let nextVoiceTurnId = 0
  let currentTurn: VoiceTurn | null = null

  function pruneSettledPublications(turn: VoiceTurn): void {
    for (const publication of turn.publications) {
      if (publication.owner.settled) turn.publications.delete(publication)
    }
  }

  function turnOwnsContext(
    turn: VoiceTurn,
    phase: VoiceTurnPhase,
    bubbleId: string | null = turn.replyBubbleId,
  ): boolean {
    return !lifecycleOwner.disposed
      && currentTurn === turn
      && turn.closure === null
      && turn.phase === phase
      && turn.replyBubbleId === bubbleId
  }

  function turnOwnsClosure(turn: VoiceTurn, closure: TurnClosure): boolean {
    return currentTurn === turn && turn.closure === closure
  }

  function ownsReveal(
    turn: VoiceTurn,
    current: RevealOwnership,
    bubbleId: string,
    index: number,
  ): boolean {
    return turnOwnsContext(turn, "playing", bubbleId)
      && turn.reveal === current
      && turn.revealGeneration === current.generation
      && current.active
      && current.index === index
  }

  function isOpenTurn(turn: VoiceTurn | null): turn is VoiceTurn {
    return turn !== null
      && !lifecycleOwner.disposed
      && turn.closure === null
      && (turn.phase === "awaiting" || turn.phase === "playing")
  }

  function createTurn(): VoiceTurn {
    const id = ++nextVoiceTurnId
    return {
      id,
      timerOwner: createResourceOwner(`PiP voice turn ${id} timers`),
      publications: new Set(),
      phase: "opening",
      replyBubbleId: null,
      parkedReply: null,
      reveal: null,
      revealGeneration: 0,
      ttsWatchdog: null,
      ttsWatchdogGeneration: 0,
      closure: null,
    }
  }

  function acquireMessagePublication(
    turn: VoiceTurn,
    id: string,
    role: "user" | "assistant",
    text: string,
    attempt: ResourceReleaseAttempt,
  ): PendingMessagePublication {
    const entry: MessagePublication = {
      owner: createResourceOwner(`PiP message publication ${id}`),
      state: "acquiring",
    }
    turn.publications.add(entry)

    try {
      const lease = entry.owner.acquire<void>(
        () => opts.messages.append({ id, role, text }),
        () => {
          opts.messages.remove(id)
        },
        `PiP message ${id}`,
        attempt,
      )
      return { entry, lease }
    } catch (error) {
      entry.state = "rollback"
      pruneSettledPublications(turn)
      throw error
    }
  }

  function commitMessagePublication(
    turn: VoiceTurn,
    publication: PendingMessagePublication,
  ): void {
    publication.lease.transfer()
    turn.publications.delete(publication.entry)
  }

  function rollbackMessagePublication(
    turn: VoiceTurn,
    publication: PendingMessagePublication,
    attempt: ResourceReleaseAttempt,
  ): void {
    publication.entry.state = "rollback"
    try {
      publication.entry.owner.dispose(attempt)
    } finally {
      pruneSettledPublications(turn)
    }
  }

  function releaseMessagePublicationDebt(
    turn: VoiceTurn,
    attempt: ResourceReleaseAttempt,
  ): void {
    const failures: unknown[] = []
    for (const publication of [...turn.publications]) {
      publication.state = "rollback"
      try {
        publication.owner.dispose(attempt)
      } catch (error) {
        failures.push(error)
      }
    }
    pruneSettledPublications(turn)
    throwFailures(failures, `Failed to roll back PiP turn ${turn.id} messages`)
  }

  function finalizeTurnClosureIfReady(
    turn: VoiceTurn,
    closure: TurnClosure,
  ): boolean {
    pruneSettledPublications(turn)
    if (
      !turn.timerOwner.settled
      || turn.publications.size > 0
      || !closure.parkedReplySettled
    ) {
      return false
    }

    turn.ttsWatchdog = null
    turn.reveal = null
    turn.replyBubbleId = null
    if (currentTurn === turn) currentTurn = null
    return true
  }

  function ensureReplyBubble(
    turn: VoiceTurn,
    ownsOperation: () => boolean,
    attempt: ResourceReleaseAttempt,
  ): string | null {
    const previousPhase = turn.phase
    const existingId = turn.replyBubbleId
    if (existingId) {
      const exists = opts.messages.has(existingId)
      if (!ownsOperation() || turn.phase !== previousPhase) return null
      if (exists) return existingId
      turn.replyBubbleId = null
    }

    if (!ownsOperation() || turn.replyBubbleId !== null) return null

    const id = `voice-reply-${turn.id}`
    turn.phase = "publishing"
    let publication: PendingMessagePublication
    try {
      publication = acquireMessagePublication(
        turn,
        id,
        "assistant",
        "",
        attempt,
      )
    } catch (error) {
      if (error instanceof ResourceOwnerClosedError && !ownsOperation()) {
        return null
      }
      if (currentTurn === turn && turn.phase === "publishing") {
        turn.phase = "inactive"
      }
      throw error
    }

    if (!ownsOperation() || turn.phase !== "publishing") {
      rollbackMessagePublication(turn, publication, attempt)
      return null
    }

    turn.replyBubbleId = id
    try {
      opts.displayedAgentText.value = ""
    } catch (error) {
      if (currentTurn === turn && turn.replyBubbleId === id) {
        turn.replyBubbleId = null
        turn.phase = "inactive"
      }
      try {
        rollbackMessagePublication(turn, publication, attempt)
      } catch (cleanupError) {
        throw new AggregateError(
          [error, cleanupError],
          "PiP reply bubble publication and rollback failed",
        )
      }
      throw error
    }

    if (!ownsOperation() || turn.phase !== "publishing") {
      if (turn.replyBubbleId === id) turn.replyBubbleId = null
      rollbackMessagePublication(turn, publication, attempt)
      return null
    }

    turn.phase = previousPhase
    commitMessagePublication(turn, publication)
    return id
  }

  function clearTtsWatchdog(
    turn: VoiceTurn,
    attempt: ResourceReleaseAttempt,
  ): void {
    turn.ttsWatchdogGeneration++
    const current = turn.ttsWatchdog
    if (!current) return
    current.cancel(attempt)
    if (!current.active && turn.ttsWatchdog === current) {
      turn.ttsWatchdog = null
    }
    const closure = turn.closure
    if (closure) finalizeTurnClosureIfReady(turn, closure)
  }

  function releaseRevealTimer(
    turn: VoiceTurn,
    attempt: ResourceReleaseAttempt,
  ): ReleasedReveal | null {
    const current = turn.reveal
    if (!current) return null

    const wasActive = current.active
    current.active = false
    current.timerGeneration++
    const cleanupFailures: unknown[] = []
    const timer = current.timer
    if (timer) {
      try {
        timer.cancel(attempt)
      } catch (error) {
        cleanupFailures.push(error)
      }
      if (!timer.active && current.timer === timer) current.timer = null
    }
    if (current.timer === null && turn.reveal === current) turn.reveal = null
    const closure = turn.closure
    if (closure) finalizeTurnClosureIfReady(turn, closure)
    return { ownership: current, wasActive, cleanupFailures }
  }

  function abandonReveal(
    turn: VoiceTurn,
    current: RevealOwnership,
    bubbleId: string,
  ): void {
    if (turn.replyBubbleId !== bubbleId || turn.reveal !== current) return
    current.active = false
    current.timerGeneration++
    turn.reveal = null
    if (currentTurn === turn && turn.closure === null) turn.phase = "inactive"
  }

  function scheduleRevealStep(
    turn: VoiceTurn,
    current: RevealOwnership,
    acquisitionAttempt: ResourceReleaseAttempt,
  ): void {
    const generation = ++current.timerGeneration
    let next: TimerLease
    try {
      next = createOwnedTimeout(
        turn.timerOwner,
        "PiP subtitle reveal delay",
        callbackAttempt => {
          if (
            turn.timerOwner.disposed
            || currentTurn !== turn
            || turn.closure !== null
            || turn.reveal !== current
            || turn.revealGeneration !== current.generation
            || !current.active
            || current.timerGeneration !== generation
          ) {
            return
          }
          current.timerGeneration++
          const ownedTimer = current.timer
          if (ownedTimer && !ownedTimer.active) current.timer = null
          runRevealStep(turn, current, callbackAttempt)
        },
        current.delay,
        acquisitionAttempt,
      )
    } catch (setupError) {
      if (current.timerGeneration === generation) current.timerGeneration++
      const closure = turn.closure
      if (setupError instanceof ResourceOwnerClosedError && closure) {
        finalizeTurnClosureIfReady(turn, closure)
        return
      }
      return closeAfterSetupFailure(
        turn,
        setupError,
        acquisitionAttempt,
        "PiP subtitle reveal delay",
      )
    }

    if (
      !turn.timerOwner.disposed
      && currentTurn === turn
      && turn.closure === null
      && turn.reveal === current
      && turn.revealGeneration === current.generation
      && current.active
      && current.timerGeneration === generation
      && next.active
    ) {
      current.timer = next
    }
  }

  function runRevealStep(
    turn: VoiceTurn,
    current: RevealOwnership,
    attempt: ResourceReleaseAttempt,
  ): void {
    const bubbleId = turn.replyBubbleId
    const index = current.index
    if (!bubbleId || !ownsReveal(turn, current, bubbleId, index)) return

    const character = current.text[index]
    if (character === undefined) {
      current.active = false
      if (turn.reveal === current) turn.reveal = null
      return
    }

    let appended: boolean
    try {
      appended = opts.messages.appendText(bubbleId, character)
    } catch (error) {
      abandonReveal(turn, current, bubbleId)
      throw error
    }
    if (!ownsReveal(turn, current, bubbleId, index)) return
    if (!appended) {
      abandonReveal(turn, current, bubbleId)
      throw new Error(`PiP reply bubble ${bubbleId} disappeared during reveal`)
    }

    const displayedText = opts.displayedAgentText.value
    if (!ownsReveal(turn, current, bubbleId, index)) return
    try {
      opts.displayedAgentText.value = displayedText + character
    } catch (error) {
      abandonReveal(turn, current, bubbleId)
      throw error
    }
    if (!ownsReveal(turn, current, bubbleId, index)) return

    current.index = index + 1
    if (current.index >= current.text.length) {
      current.active = false
      if (turn.reveal === current) turn.reveal = null
      return
    }

    scheduleRevealStep(turn, current, attempt)
  }

  function finishRevealTail(
    turn: VoiceTurn,
    segmentGeneration: number,
    attempt: ResourceReleaseAttempt,
  ): void {
    // A newer segment arrived mid-reveal — audio 已前進，直接補完舊段落，字幕才不落後。
    const bubbleId = turn.replyBubbleId
    const released = releaseRevealTimer(turn, attempt)
    if (!released) return

    const failures: unknown[] = []
    failures.push(...released.cleanupFailures)
    const current = released.ownership
    const index = current.index
    const stillOwnsTail = () => bubbleId !== null
      && turnOwnsContext(turn, "playing", bubbleId)
      && turn.revealGeneration === segmentGeneration
      && current.index === index

    if (released.wasActive && bubbleId !== null && stillOwnsTail()) {
      const rest = current.text.slice(index)
      if (rest) {
        try {
          const appended = opts.messages.appendText(bubbleId, rest)
          if (stillOwnsTail()) {
            if (!appended) {
              turn.phase = "inactive"
              failures.push(
                new Error(`PiP reply bubble ${bubbleId} disappeared during reveal`),
              )
            } else {
              const displayedText = opts.displayedAgentText.value
              if (stillOwnsTail()) {
                opts.displayedAgentText.value = displayedText + rest
                if (stillOwnsTail()) current.index = current.text.length
              }
            }
          }
        } catch (error) {
          if (stillOwnsTail()) turn.phase = "inactive"
          failures.push(error)
        }
      }
    }
    throwFailures(failures, "Failed to finish PiP subtitle reveal")
  }

  function applyParkedReply(
    turn: VoiceTurn,
    closure: TurnClosure,
    attempt: ResourceReleaseAttempt,
  ): void {
    if (closure.parkedReplySettled) return
    if (!closure.flushParkedReply || lifecycleOwner.disposed) {
      turn.parkedReply = null
      closure.parkedReplySettled = true
      return
    }
    if (turn.parkedReply === null) {
      closure.parkedReplySettled = true
      return
    }

    const text = turn.parkedReply
    const ownsOperation = () => currentTurn === turn
      && turn.closure === closure
    const bubbleId = ensureReplyBubble(turn, ownsOperation, attempt)
    if (!bubbleId || turn.parkedReply !== text || !ownsOperation()) return

    const updated = opts.messages.setText(bubbleId, text)
    if (turn.parkedReply !== text || !ownsOperation()) return
    if (!updated) {
      throw new Error(`PiP reply bubble ${bubbleId} disappeared before publication`)
    }

    opts.displayedAgentText.value = text
    if (turn.parkedReply === text && ownsOperation()) {
      turn.parkedReply = null
      closure.parkedReplySettled = true
    }
  }

  function publishTurnClosure(
    turn: VoiceTurn,
    nextPhase: "inactive" | "settled",
    flushParkedReply: boolean,
  ): TurnClosure {
    const existing = turn.closure
    if (existing) return existing

    const closure: TurnClosure = {
      nextPhase,
      flushParkedReply,
      running: false,
      lastAttempt: null,
      failed: false,
      lastFailure: undefined,
      parkedReplySettled: !flushParkedReply || turn.parkedReply === null,
    }
    turn.closure = closure
    turn.phase = nextPhase
    turn.ttsWatchdogGeneration++
    turn.revealGeneration++
    if (turn.reveal) {
      turn.reveal.active = false
      turn.reveal.timerGeneration++
    }
    if (!flushParkedReply) turn.parkedReply = null
    return closure
  }

  function attemptTurnClosure(
    turn: VoiceTurn,
    closure: TurnClosure,
    attempt: ResourceReleaseAttempt,
  ): boolean {
    if (!turnOwnsClosure(turn, closure)) return true
    if (closure.running) return false
    if (closure.lastAttempt === attempt) {
      if (closure.failed) throw closure.lastFailure
      return finalizeTurnClosureIfReady(turn, closure)
    }

    closure.running = true
    closure.lastAttempt = attempt
    closure.failed = false
    closure.lastFailure = undefined
    const failures: unknown[] = []

    try {
      releaseMessagePublicationDebt(turn, attempt)
    } catch (error) {
      failures.push(error)
    }

    if (turn.publications.size === 0) {
      try {
        applyParkedReply(turn, closure, attempt)
      } catch (error) {
        failures.push(error)
      }
    }

    try {
      turn.timerOwner.dispose(attempt)
    } catch (error) {
      failures.push(error)
    }

    closure.running = false
    if (failures.length > 0) {
      try {
        throwFailures(failures, `Failed to close PiP voice turn ${turn.id}`)
      } catch (error) {
        closure.failed = true
        closure.lastFailure = error
        throw error
      }
    }

    return finalizeTurnClosureIfReady(turn, closure)
  }

  function closeTurn(
    turn: VoiceTurn,
    nextPhase: "inactive" | "settled",
    flushParkedReply: boolean,
    attempt: ResourceReleaseAttempt,
  ): boolean {
    const closure = publishTurnClosure(turn, nextPhase, flushParkedReply)
    return attemptTurnClosure(turn, closure, attempt)
  }

  function closeAfterSetupFailure(
    turn: VoiceTurn,
    setupError: unknown,
    attempt: ResourceReleaseAttempt,
    lifecycle: string,
  ): never {
    let cleanupOutcome: { failed: false } | { failed: true; failure: unknown } = {
      failed: false,
    }
    try {
      closeTurn(turn, "inactive", false, attempt)
    } catch (error) {
      cleanupOutcome = { failed: true, failure: error }
    }
    if (cleanupOutcome.failed) {
      throw new AggregateError(
        [setupError, cleanupOutcome.failure],
        `Failed to initialize and close ${lifecycle}`,
      )
    }
    throw setupError
  }

  function resumePendingClosure(attempt: ResourceReleaseAttempt): void {
    const turn = currentTurn
    const closure = turn?.closure
    if (!turn || !closure || closure.running) return
    attemptTurnClosure(turn, closure, attempt)
  }

  function runOperation<T>(
    operation: (attempt: ResourceReleaseAttempt) => T,
  ): T {
    const attempt = currentSynchronousScopeReleaseAttempt() ?? {}
    return runWithSynchronousScopeReleaseAttempt(attempt, () => {
      resumePendingClosure(attempt)
      return operation(attempt)
    })
  }

  function prepareTurn(attempt: ResourceReleaseAttempt): VoiceTurn | null {
    if (lifecycleOwner.disposed) return null
    const previous = currentTurn
    if (previous) {
      if (!closeTurn(previous, "inactive", false, attempt)) return null
    }
    if (lifecycleOwner.disposed || currentTurn !== null) return null

    const turn = createTurn()
    currentTurn = turn
    return turn
  }

  function commitTurn(turn: VoiceTurn): boolean {
    if (!turnOwnsContext(turn, "opening", null)) return false
    turn.phase = "awaiting"
    return true
  }

  /** Open the assistant-only welcome turn before the WebRTC pipeline starts. */
  function beginVoiceSession(): boolean {
    return runOperation(attempt => {
      const turn = prepareTurn(attempt)
      return turn !== null && commitTurn(turn)
    })
  }

  /** Open a fresh user turn and detach the prior turn's bubble. */
  function beginVoiceTurn(text: string): boolean {
    return runOperation(attempt => {
      const turn = prepareTurn(attempt)
      if (!turn) return false
      const messageId = `voice-user-${turn.id}`
      let publication: PendingMessagePublication
      try {
        publication = acquireMessagePublication(
          turn,
          messageId,
          "user",
          text,
          attempt,
        )
      } catch (error) {
        const closure = turn.closure
        if (error instanceof ResourceOwnerClosedError && closure) {
          finalizeTurnClosureIfReady(turn, closure)
          return false
        }
        return closeAfterSetupFailure(
          turn,
          error,
          attempt,
          "PiP user bubble publication",
        )
      }

      if (!commitTurn(turn)) {
        rollbackMessagePublication(turn, publication, attempt)
        const closure = turn.closure
        if (closure) finalizeTurnClosureIfReady(turn, closure)
        return false
      }
      commitMessagePublication(turn, publication)
      return true
    })
  }

  function startTtsWatchdog(
    turn: VoiceTurn,
    attempt: ResourceReleaseAttempt,
  ): boolean {
    clearTtsWatchdog(turn, attempt)
    if (!turnOwnsContext(turn, "awaiting")) return false

    const generation = ++turn.ttsWatchdogGeneration
    let next: TimerLease
    try {
      next = createOwnedTimeout(
        turn.timerOwner,
        "PiP TTS watchdog",
        callbackAttempt => {
          if (
            lifecycleOwner.disposed
            || currentTurn !== turn
            || turn.closure !== null
            || turn.ttsWatchdogGeneration !== generation
            || turn.phase !== "awaiting"
            || turn.parkedReply === null
          ) {
            return
          }
          turn.ttsWatchdogGeneration++
          const current = turn.ttsWatchdog
          if (current && !current.active) turn.ttsWatchdog = null

          if (closeTurn(turn, "settled", true, callbackAttempt)) {
            runWithSynchronousScopeReleaseAttempt(
              callbackAttempt,
              opts.onWatchdogExpire,
            )
          }
        },
        TTS_WATCHDOG_MS,
        attempt,
      )
    } catch (setupError) {
      if (turn.ttsWatchdogGeneration === generation) {
        turn.ttsWatchdogGeneration++
      }
      const closure = turn.closure
      if (setupError instanceof ResourceOwnerClosedError && closure) {
        finalizeTurnClosureIfReady(turn, closure)
        return false
      }
      return closeAfterSetupFailure(
        turn,
        setupError,
        attempt,
        "PiP TTS watchdog",
      )
    }

    if (
      !turn.timerOwner.disposed
      && turnOwnsContext(turn, "awaiting")
      && turn.ttsWatchdogGeneration === generation
      && next.active
    ) {
      turn.ttsWatchdog = next
      return true
    }
    return false
  }

  function receiveReply(text: string): boolean {
    return runOperation(attempt => {
      const turn = currentTurn
      if (!isOpenTurn(turn)) return false
      const expectedPhase = turn.phase
      turn.parkedReply = text
      if (expectedPhase === "playing") {
        clearTtsWatchdog(turn, attempt)
        return turnOwnsContext(turn, "playing")
      }
      return startTtsWatchdog(turn, attempt)
    })
  }

  function markAudioStartedWithAttempt(
    attempt: ResourceReleaseAttempt,
  ): VoiceTurn | null {
    const turn = currentTurn
    if (!isOpenTurn(turn)) return null
    const expectedPhase = turn.phase
    clearTtsWatchdog(turn, attempt)
    if (!turnOwnsContext(turn, expectedPhase)) return null
    turn.phase = "playing"
    return turn
  }

  function markAudioStarted(): boolean {
    return runOperation(attempt => markAudioStartedWithAttempt(attempt) !== null)
  }

  function revealSegment(text: string, durationMs: number): boolean {
    return runOperation(attempt => {
      const turn = markAudioStartedWithAttempt(attempt)
      if (!turn) return false
      const segmentGeneration = ++turn.revealGeneration
      finishRevealTail(turn, segmentGeneration, attempt)
      if (!turnOwnsContext(turn, "playing")
        || turn.revealGeneration !== segmentGeneration) {
        return false
      }

      const ownsSegment = () => !lifecycleOwner.disposed
        && currentTurn === turn
        && turn.closure === null
        && turn.revealGeneration === segmentGeneration
      if (!ensureReplyBubble(turn, ownsSegment, attempt)) return false
      if (!ownsSegment() || turn.phase !== "playing") return false
      if (!text) return true

      const current: RevealOwnership = {
        text,
        index: 0,
        delay: Math.max(15, durationMs / text.length),
        active: true,
        generation: segmentGeneration,
        timerGeneration: 0,
        timer: null,
      }
      turn.reveal = current
      runRevealStep(turn, current, attempt)
      return currentTurn === turn
        && turn.closure === null
        && turn.phase === "playing"
        && turn.revealGeneration === segmentGeneration
    })
  }

  /** Barge-in ends playback ownership but preserves the already-spoken bubble. */
  function cancelVoiceReply(): boolean {
    return runOperation(attempt => {
      const turn = currentTurn
      if (!isOpenTurn(turn)) return false
      return closeTurn(turn, "settled", false, attempt)
    })
  }

  /** Consume one matching bot_silent. Stale/duplicate silence is a no-op. */
  function finishVoiceReply(): boolean {
    return runOperation(attempt => {
      const turn = currentTurn
      if (!turn || turn.closure !== null || turn.phase !== "playing") return false
      return closeTurn(turn, "settled", true, attempt)
    })
  }

  function resetVoiceReply(): void {
    runOperation(attempt => {
      const turn = currentTurn
      if (turn) closeTurn(turn, "inactive", false, attempt)
    })
  }

  observeSynchronousScopeTeardown("PiP voice reveal teardown", attempt => {
    const failures: unknown[] = []
    try {
      lifecycleOwner.dispose(attempt)
    } catch (error) {
      failures.push(error)
    }

    const turn = currentTurn
    if (turn) {
      const closure = publishTurnClosure(turn, "inactive", false)
      try {
        attemptTurnClosure(turn, closure, attempt)
      } catch (error) {
        failures.push(error)
      }
      if (currentTurn === turn && failures.length === 0) {
        failures.push(
          new Error(`PiP voice turn ${turn.id} cleanup remains unresolved`),
        )
      }
    }

    if (!lifecycleOwner.settled && failures.length === 0) {
      failures.push(new Error("PiP voice reveal lifecycle cleanup remains unresolved"))
    }
    throwFailures(failures, "Failed to tear down PiP voice reveal")
  })

  return {
    beginVoiceSession,
    beginVoiceTurn,
    revealSegment,
    resetVoiceReply,
    receiveReply,
    cancelVoiceReply,
    markAudioStarted,
    finishVoiceReply,
  }
}
