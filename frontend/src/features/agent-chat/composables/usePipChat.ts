import { ref, type Ref } from "vue"

import { UI_FALLBACK_MESSAGES } from "@/lib/api-messages"
import { observeAsynchronousScopeTeardown } from "@/lib/component-lifecycle"
import { reportClientEvent } from "@/lib/report-client-event"
import {
  createAsyncReleaseOwner,
  type AsyncReleaseAction,
  type AsyncReleaseOwner,
} from "@/lib/async-release-owner"
import {
  createResourceOwner,
  ResourceAcquisitionRollbackError,
  ResourceOwnerClosedError,
  type ResourceClaim,
  type ResourceOwner,
  type ResourceReleaseAttempt,
} from "@/lib/resource-owner"
import { createOwnedTimeout, type TimerLease } from "@/lib/timer-owner"

import {
  ChatApiError,
  createChatSession,
  deleteChatSession,
  sendChatMessageStream,
} from "../api/chat"
import { usePipMessageStore } from "./usePipMessageStore"
import { useTts } from "./useTts"

let messageCounter = 0
const RESOLVED_VOID = Promise.resolve()

function nextMessageId(): string {
  messageCounter += 1
  return `${Date.now()}-${messageCounter}`
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

function reportLifecycleFailure(type: string, error: unknown) {
  try {
    reportClientEvent(type, errorMessage(error))
  } catch {
    // Reporting is best-effort. The authoritative failure remains retained by
    // the lifecycle owner and is retried/surfaced through endSession().
  }
}

export type PipChatSessionHandle = Readonly<{
  id: string
  generation: number
}>

type TypewriterOwnership = {
  generation: number
  text: string
  index: number
  delay: number
  resources: ResourceOwner
  timer: TimerLease | null
  timerGeneration: object | null
}

type SpeechOwnership = {
  generation: number
  operation: Promise<void>
}

type SessionOwnership = {
  id: string
  generation: number
  controller: AbortController | null
  cancellationResources: ResourceOwner
  pendingTeardownFailures: unknown[]
  creationStarted: boolean
  createOperation: Promise<void> | null
  readyOperation: Promise<void> | null
  readyResult: PipChatSessionHandle | null
  readyFailed: boolean
  readyFailure: unknown
  retirementRequested: boolean
  retirementAttempt: ResourceReleaseAttempt | null
  releases: AsyncReleaseOwner
}

type SendOwnership = {
  generation: number
  controller: AbortController | null
  cancellationResources: ResourceOwner
  cancellationClaim: ResourceClaim | null
  pendingTeardownFailures: unknown[]
  retirementRequested: boolean
  retirementOperation: Promise<void> | null
  operationFailed: boolean
  operationFailure: unknown
  operation: Promise<void> | null
}

export function usePipChat(
  suppressTts: Readonly<Ref<boolean>> = ref(false),
  onActivity: () => void = () => {},
) {
  const { messages, writer: messageWriter } = usePipMessageStore()
  const userInput = ref("")
  const isSending = ref(false)
  const showChat = ref(false)
  const {
    ttsState,
    mouthAmplitude,
    speak: speakTts,
    release: releaseTts,
  } = useTts()

  const displayedAgentText = ref("")

  let lifecycleActive = false
  let sessionGeneration = 0

  let typewriterOwnership: TypewriterOwnership | null = null
  let activeSpeech: SpeechOwnership | null = null
  const speechOperations = new Set<SpeechOwnership>()
  const ttsReleases = createAsyncReleaseOwner("PiP chat TTS resources")

  let sessionOwnership: SessionOwnership | null = null
  const retiredSessions = new Set<SessionOwnership>()

  let activeSend: SendOwnership | null = null
  const retiredSends = new Set<SendOwnership>()

  let cleanupRequested = false
  let cleanupOperation: Promise<void> | null = null
  let cleanupFailed = false
  let cleanupFailure: unknown
  const pendingTeardownFailures: unknown[] = []

  const lifetimeOwner = createResourceOwner("PiP chat component")

  function closeLifetimeOwner(attempt: ResourceReleaseAttempt): void {
    if (lifetimeOwner.disposed) return
    try {
      lifetimeOwner.close(undefined, attempt)
    } catch (error) {
      cleanupRequested = true
      pendingTeardownFailures.push(new Error(
        `PiP chat close gate: ${errorMessage(error)}`,
        { cause: error },
      ))
    }
  }

  function ownsTypewriter(owner: TypewriterOwnership): boolean {
    return !lifetimeOwner.disposed
      && lifecycleActive
      && typewriterOwnership === owner
      && owner.generation === sessionGeneration
      && !owner.resources.disposed
  }

  function releaseTypewriter(
    owner: TypewriterOwnership,
    attempt: ResourceReleaseAttempt,
  ): void {
    owner.timerGeneration = null
    owner.resources.dispose(attempt)
    if (owner.timer && !owner.timer.active) owner.timer = null
    if (owner.resources.settled && typewriterOwnership === owner) {
      typewriterOwnership = null
    }
  }

  function detachTypewriter(attempt: ResourceReleaseAttempt): void {
    const owner = typewriterOwnership
    if (owner) releaseTypewriter(owner, attempt)
  }

  function runTypewriterStep(
    owner: TypewriterOwnership,
    attempt: ResourceReleaseAttempt,
  ): void {
    if (!ownsTypewriter(owner)) return

    const character = owner.text[owner.index]
    if (character === undefined) {
      releaseTypewriter(owner, attempt)
      return
    }

    displayedAgentText.value += character
    owner.index++
    if (owner.index >= owner.text.length) {
      releaseTypewriter(owner, attempt)
      return
    }

    const generation = {}
    owner.timerGeneration = generation
    const timer = createOwnedTimeout(
      owner.resources,
      "PiP chat typewriter step",
      (callbackAttempt) => {
        if (owner.timerGeneration !== generation) return
        owner.timerGeneration = null
        owner.timer = null
        runTypewriterStep(owner, callbackAttempt)
      },
      owner.delay,
      attempt,
    )
    if (owner.timerGeneration === generation && timer.active) owner.timer = timer
  }

  function startTypewriter(text: string, durationMs?: number): void {
    const attempt: ResourceReleaseAttempt = {}
    detachTypewriter(attempt)
    displayedAgentText.value = ""
    if (lifetimeOwner.disposed || !lifecycleActive || !text) return

    const owner: TypewriterOwnership = {
      generation: sessionGeneration,
      text,
      index: 0,
      delay: durationMs && text.length ? Math.max(15, durationMs / text.length) : 35,
      resources: createResourceOwner("PiP chat typewriter"),
      timer: null,
      timerGeneration: null,
    }
    typewriterOwnership = owner
    runTypewriterStep(owner, attempt)
  }

  function invalidateSpeechContinuation(attempt: ResourceReleaseAttempt = {}): void {
    activeSpeech = null
    detachTypewriter(attempt)
  }

  function ownsSpeech(owner: SpeechOwnership): boolean {
    return !lifetimeOwner.disposed
      && lifecycleActive
      && activeSpeech === owner
      && owner.generation === sessionGeneration
  }

  function ownTtsSessionResources(): void {
    if (!ttsReleases.settled) return
    ttsReleases.claim("TTS playback", () => releaseTts())
  }

  function startSpeech(text: string) {
    if (lifetimeOwner.disposed || !lifecycleActive) return

    invalidateSpeechContinuation()
    let owner!: SpeechOwnership
    const operation = Promise.resolve().then(async () => {
      try {
        if (!ownsSpeech(owner)) return
        if (suppressTts.value) {
          startTypewriter(text)
          return
        }

        // The session-level release claim is published before speakTts can enter
        // browser/audio host code or synchronously re-enter teardown.
        ownTtsSessionResources()
        if (!ownsSpeech(owner)) return
        const durationMs = await speakTts(text)
        if (ownsSpeech(owner)) startTypewriter(text, durationMs ?? undefined)
      } catch (error) {
        cleanupRequested = true
        pendingTeardownFailures.push(error)
        reportLifecycleFailure("pip_chat_speech_cleanup_error", error)
      } finally {
        speechOperations.delete(owner)
        if (activeSpeech === owner) activeSpeech = null
      }
    })
    owner = { generation: sessionGeneration, operation }
    activeSpeech = owner
    speechOperations.add(owner)
  }

  function clearDisplayedText() {
    invalidateSpeechContinuation()
    displayedAgentText.value = ""
  }

  function acquireOwnedCancellation(
    resources: ResourceOwner,
    label: string,
    attempt: ResourceReleaseAttempt,
  ) {
    return resources.acquire(
      () => new AbortController(),
      controller => controller?.abort(),
      `${label} request cancellation`,
      attempt,
    )
  }

  function requestOwnedCancellation(
    owner: {
      cancellationResources: ResourceOwner
      pendingTeardownFailures: unknown[]
    },
    resource: string,
    attempt: ResourceReleaseAttempt,
  ) {
    try {
      owner.cancellationResources.dispose(attempt)
    } catch (error) {
      owner.pendingTeardownFailures.push(
        new Error(`${resource}: ${errorMessage(error)}`, { cause: error }),
      )
    }
  }

  function createSessionOwnership(
    id: string,
    generation: number,
  ): SessionOwnership {
    const owner: SessionOwnership = {
      id,
      generation,
      controller: null,
      cancellationResources: createResourceOwner("PiP chat session"),
      pendingTeardownFailures: [],
      creationStarted: false,
      createOperation: null,
      readyOperation: null,
      readyResult: null,
      readyFailed: false,
      readyFailure: undefined,
      retirementRequested: false,
      retirementAttempt: null,
      releases: createAsyncReleaseOwner(`PiP chat session ${id}`),
    }
    owner.releases.claim("DELETE", async () => {
      const creation = owner.createOperation
      if (creation) await Promise.allSettled([creation])
      if (owner.creationStarted) await deleteChatSession(owner.id)
    })
    return owner
  }

  function requestSessionRetirement(
    owner: SessionOwnership,
    attempt: ResourceReleaseAttempt = {},
  ): ResourceReleaseAttempt {
    if (sessionOwnership === owner) sessionOwnership = null
    if (owner.retirementRequested) return owner.retirementAttempt ?? attempt

    owner.retirementRequested = true
    owner.retirementAttempt = attempt
    retiredSessions.add(owner)
    cleanupRequested = true
    requestOwnedCancellation(owner, "chat session request cancellation", attempt)
    return attempt
  }

  async function settleSessionRetirement(
    owner: SessionOwnership,
    attempt: ResourceReleaseAttempt,
  ): Promise<void> {
    const failures = owner.pendingTeardownFailures.splice(0)
    try {
      owner.cancellationResources.dispose(attempt)
    } catch (error) {
      failures.push(new Error(
        `chat session request cancellation: ${errorMessage(error)}`,
        { cause: error },
      ))
    }

    if (
      (!owner.controller || !owner.controller.signal.aborted)
      && !owner.cancellationResources.settled
    ) {
      if (failures.length === 0) {
        failures.push(new Error("Chat session cancellation debt remains unresolved"))
      }
      throw new AggregateError(failures, "Chat session cancellation debt remains")
    }

    try {
      await owner.releases.settle(attempt)
    } catch (error) {
      failures.push(error)
    }

    if (owner.cancellationResources.settled && owner.releases.settled) {
      retiredSessions.delete(owner)
    }
    if (failures.length > 0) {
      throw new AggregateError(failures, "Chat session teardown reported a failure")
    }
    if (!owner.cancellationResources.settled || !owner.releases.settled) {
      throw new Error("Chat session teardown debt remains unresolved")
    }
  }

  async function settleRetiredSessions(
    attempt: ResourceReleaseAttempt,
    owners: readonly SessionOwnership[] = [...retiredSessions],
  ): Promise<void> {
    if (owners.length === 0) return

    const results = await Promise.allSettled(
      owners.map(owner => settleSessionRetirement(owner, attempt)),
    )
    const failures = results.flatMap(result =>
      result.status === "rejected" ? [result.reason] : [],
    )
    if (failures.length > 0) {
      throw new AggregateError(failures, "Chat session deletion debt remains")
    }
  }

  function sessionIsStale(owner: SessionOwnership): boolean {
    return lifetimeOwner.disposed
      || !lifecycleActive
      || sessionOwnership !== owner
      || owner.generation !== sessionGeneration
      || !owner.controller
      || owner.controller.signal.aborted
  }

  async function finishSessionCreation(owner: SessionOwnership): Promise<PipChatSessionHandle | null> {
    const creation = owner.createOperation
    let createOutcome: { failed: false } | { failed: true; failure: unknown } = {
      failed: false,
    }
    try {
      await creation
    } catch (error) {
      createOutcome = { failed: true, failure: error }
    } finally {
      if (owner.createOperation === creation) owner.createOperation = null
    }

    if (createOutcome.failed) {
      const createFailure = createOutcome.failure
      const stale = sessionIsStale(owner)
      const retirementAttempt = requestSessionRetirement(owner)

      if (!stale && !messages.value.length) {
        messageWriter.append({
          id: "session-error",
          role: "assistant",
          text: `（${UI_FALLBACK_MESSAGES.agentOffline}）`,
        })
      }

      try {
        await settleSessionRetirement(owner, retirementAttempt)
      } catch (cleanupError) {
        if (stale) throw cleanupError
        throw new AggregateError(
          [createFailure, cleanupError],
          "Chat session creation failed and its client ID could not be deleted",
        )
      }
      return null
    }

    if (sessionIsStale(owner)) {
      const retirementAttempt = requestSessionRetirement(owner)
      await settleSessionRetirement(owner, retirementAttempt)
      return null
    }

    if (!messages.value.length && !suppressTts.value) {
      const welcomeText = "請問您欲前往哪裡？"
      messageWriter.append({ id: "welcome", role: "assistant", text: welcomeText })
      startSpeech(welcomeText)
    }
    return { id: owner.id, generation: owner.generation }
  }

  function startSessionCreation(owner: SessionOwnership) {
    const controller = owner.controller
    if (!controller || owner.retirementRequested) return

    owner.creationStarted = true
    owner.createOperation = Promise.resolve().then(
      () => createChatSession(owner.id, controller.signal),
    )

    let readyOperation!: Promise<void>
    readyOperation = Promise.resolve().then(async () => {
      try {
        owner.readyResult = await finishSessionCreation(owner)
      } catch (error) {
        owner.readyFailed = true
        owner.readyFailure = error
        cleanupRequested = true
        pendingTeardownFailures.push(error)
        reportLifecycleFailure("pip_chat_session_cleanup_error", error)
      } finally {
        if (owner.readyOperation === readyOperation) owner.readyOperation = null
      }
    })
    owner.readyOperation = readyOperation
  }

  async function waitForSession(owner: SessionOwnership): Promise<PipChatSessionHandle | null> {
    const operation = owner.readyOperation
    if (operation) await operation
    if (owner.readyFailed) throw owner.readyFailure
    return owner.readyResult
  }

  async function settleCleanupBeforeOpen() {
    const operation = cleanupOperation
      ?? (cleanupRequested ? startCleanupOperation() : null)
    if (operation) await operation
    if (cleanupFailed) throw cleanupFailure
  }

  async function ensureSession(
    expectedGeneration = sessionGeneration,
  ): Promise<PipChatSessionHandle | null> {
    const attempt: ResourceReleaseAttempt = {}
    if (lifetimeOwner.disposed || expectedGeneration !== sessionGeneration) return null

    // Publish the open intent before the first await. A close that arrives while
    // retained cleanup is settling must invalidate this exact generation.
    lifecycleActive = true
    await settleCleanupBeforeOpen()
    if (lifetimeOwner.disposed || !lifecycleActive || expectedGeneration !== sessionGeneration) return null
    const existing = sessionOwnership
    if (existing?.generation === expectedGeneration) {
      return waitForSession(existing)
    }
    if (existing) {
      requestSessionRetirement(existing, attempt)
      await settleRetiredSessions(attempt)
      if (lifetimeOwner.disposed || expectedGeneration !== sessionGeneration) return null
    }

    const id = crypto.randomUUID()
    if (lifetimeOwner.disposed || !lifecycleActive || expectedGeneration !== sessionGeneration) return null

    const owner = createSessionOwnership(id, expectedGeneration)
    sessionOwnership = owner
    try {
      const cancellation = acquireOwnedCancellation(
        owner.cancellationResources,
        "PiP chat session",
        attempt,
      )
      owner.controller = cancellation.value
    } catch (error) {
      const retiredDuringAcquisition = owner.retirementRequested
      const retirementAttempt = requestSessionRetirement(owner, attempt)

      if (retiredDuringAcquisition && !(error instanceof ResourceOwnerClosedError)) {
        owner.pendingTeardownFailures.push(error)
      }

      try {
        await settleSessionRetirement(owner, retirementAttempt)
      } catch (cleanupError) {
        if (retiredDuringAcquisition) throw cleanupError
        throw new AggregateError(
          [error, cleanupError],
          "Chat session cancellation setup failed and rollback remains incomplete",
        )
      }

      if (retiredDuringAcquisition && error instanceof ResourceOwnerClosedError) return null
      throw error
    }

    if (sessionIsStale(owner)) {
      const retirementAttempt = requestSessionRetirement(owner, attempt)
      await settleSessionRetirement(owner, retirementAttempt)
      return null
    }
    startSessionCreation(owner)
    return waitForSession(owner)
  }

  function ownsSend(turn: SendOwnership): boolean {
    return !lifetimeOwner.disposed
      && lifecycleActive
      && activeSend === turn
      && turn.generation === sessionGeneration
      && !turn.retirementRequested
      && turn.controller !== null
      && !turn.controller.signal.aborted
  }

  async function runSend(turn: SendOwnership, text: string, id: string) {
    try {
      if (!ownsSend(turn)) return
      onActivity()
      const session = await ensureSession(turn.generation)
      if (!session || !ownsSend(turn)) return
      const controller = turn.controller
      if (!controller) return

      userInput.value = ""
      messageWriter.append({ id, role: "user", text })

      const replyId = `${id}-reply`
      const reply = await sendChatMessageStream(
        session.id,
        text,
        (delta) => {
          if (!ownsSend(turn)) return
          onActivity()
          if (!messageWriter.appendText(replyId, delta)) {
            messageWriter.append({ id: replyId, role: "assistant", text: delta })
          }
        },
        controller.signal,
      )
      if (!ownsSend(turn)) return
      if (!messageWriter.has(replyId)) {
        messageWriter.append({ id: replyId, role: "assistant", text: reply })
      }
      startSpeech(reply)
    } catch (error) {
      if (!ownsSend(turn)) return
      if (error instanceof DOMException && error.name === "AbortError") return

      let cleanupOutcome: { failed: false } | { failed: true; failure: unknown } = {
        failed: false,
      }
      if (error instanceof ChatApiError && error.status === 404) {
        const owner = sessionOwnership
        if (owner?.generation === turn.generation) {
          const retirementAttempt = requestSessionRetirement(owner)
          try {
            await settleSessionRetirement(owner, retirementAttempt)
          } catch (retirementError) {
            cleanupOutcome = { failed: true, failure: retirementError }
          }
        }
      }

      const message = error instanceof ChatApiError
        ? error.message
        : UI_FALLBACK_MESSAGES.agentNoReply
      const renderedMessage = `（${message}）`
      messageWriter.append({ id: `${id}-error`, role: "assistant", text: renderedMessage })
      startSpeech(renderedMessage)

      if (cleanupOutcome.failed) {
        throw new AggregateError(
          [error, cleanupOutcome.failure],
          "Chat request failed and session cleanup remains incomplete",
        )
      }
    }
  }

  function requestSendRetirement(turn: SendOwnership, attempt: ResourceReleaseAttempt = {}) {
    if (turn.retirementRequested) return
    turn.retirementRequested = true
    retiredSends.add(turn)
    cleanupRequested = true
    requestOwnedCancellation(turn, "chat send cancellation", attempt)
  }

  function settleSendRetirement(turn: SendOwnership, attempt: ResourceReleaseAttempt = {}): Promise<void> {
    if (turn.retirementOperation) return turn.retirementOperation

    const physicalOperation = Promise.resolve().then(async () => {
      const failures = turn.pendingTeardownFailures.splice(0)
      try {
        turn.cancellationResources.dispose(attempt)
      } catch (error) {
        failures.push(new Error(`chat send cancellation: ${errorMessage(error)}`, { cause: error }))
      }

      if (
        (!turn.controller || !turn.controller.signal.aborted)
        && !turn.cancellationResources.settled
      ) {
        throw new AggregateError(failures, "Chat send cancellation debt remains")
      }

      const operation = turn.operation
      if (operation) await operation
      failures.push(...turn.pendingTeardownFailures.splice(0))

      if (turn.cancellationResources.settled) retiredSends.delete(turn)
      if (failures.length > 0) {
        throw new AggregateError(failures, "Chat send teardown reported a failure")
      }
      if (!turn.cancellationResources.settled) {
        throw new Error("Chat send cancellation debt remains unresolved")
      }
    })

    let operation!: Promise<void>
    operation = physicalOperation.finally(() => {
      if (turn.retirementOperation === operation) turn.retirementOperation = null
    })
    turn.retirementOperation = operation
    return operation
  }

  async function settleRetiredSends(
    attempt: ResourceReleaseAttempt = {},
    turns: readonly SendOwnership[] = [...retiredSends],
  ): Promise<void> {
    if (turns.length === 0) return

    const results = await Promise.allSettled(turns.map(turn => settleSendRetirement(turn, attempt)))
    const failures = results.flatMap(result =>
      result.status === "rejected" ? [result.reason] : [],
    )
    if (failures.length > 0) {
      throw new AggregateError(failures, "Chat send teardown debt remains")
    }
  }

  function requestSendMessage(): SendOwnership | null {
    if (lifetimeOwner.disposed) return null
    if (activeSend) return activeSend

    const text = userInput.value.trim()
    if (!text) return null

    const generation = sessionGeneration
    lifecycleActive = true
    const turn: SendOwnership = {
      generation,
      controller: null,
      cancellationResources: createResourceOwner("PiP chat send"),
      cancellationClaim: null,
      pendingTeardownFailures: [],
      retirementRequested: false,
      retirementOperation: null,
      operationFailed: false,
      operationFailure: undefined,
      operation: null,
    }
    activeSend = turn
    isSending.value = true
    const id = nextMessageId()
    let setupOutcome: { failed: false } | { failed: true; failure: unknown } = {
      failed: false,
    }

    if (lifetimeOwner.disposed || !lifecycleActive || generation !== sessionGeneration) {
      requestSendRetirement(turn)
    } else {
      try {
        const cancellation = acquireOwnedCancellation(
          turn.cancellationResources,
          "PiP chat send",
          {},
        )
        turn.controller = cancellation.value
        turn.cancellationClaim = cancellation
      } catch (error) {
        setupOutcome = { failed: true, failure: error }
        if (turn.retirementRequested) {
          if (!(error instanceof ResourceOwnerClosedError)) {
            turn.pendingTeardownFailures.push(error)
          }
        } else if (!turn.cancellationResources.settled) {
          turn.pendingTeardownFailures.push(error)
          requestSendRetirement(turn)
        }
      }
    }

    turn.operation = Promise.resolve().then(async () => {
      try {
        if (setupOutcome.failed) {
          const setupFailure = setupOutcome.failure
          if (turn.retirementRequested && setupFailure instanceof ResourceOwnerClosedError) return
          turn.operationFailed = true
          turn.operationFailure = setupFailure
          reportLifecycleFailure("pip_chat_send_setup_error", setupFailure)
          return
        }
        await runSend(turn, text, id)
      } catch (error) {
        turn.operationFailed = true
        turn.operationFailure = error
        turn.pendingTeardownFailures.push(error)
        requestSendRetirement(turn)
        reportLifecycleFailure("pip_chat_send_cleanup_error", error)
      } finally {
        if (!turn.retirementRequested) turn.cancellationClaim?.transfer()
        if (activeSend === turn) {
          activeSend = null
          isSending.value = false
        }
      }
    })
    return turn
  }

  async function sendMessage(): Promise<void> {
    const turn = requestSendMessage()
    if (turn?.operation) await turn.operation
    if (turn?.operationFailed) {
      throw turn.operationFailure
    }
  }

  function handleKeydown(event: KeyboardEvent) {
    if (lifetimeOwner.disposed) return
    onActivity()
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault()
      requestSendMessage()
    }
  }

  function releaseLifetimeResources(attempt: ResourceReleaseAttempt): void {
    if (!lifetimeOwner.disposed || lifetimeOwner.settled) return
    lifetimeOwner.dispose(attempt)
  }

  function typewriterCleanupPending(): boolean {
    const owner = typewriterOwnership
    if (!owner) return false
    return lifetimeOwner.disposed || !lifecycleActive || owner.resources.disposed
  }

  function hasPendingCleanupResources(): boolean {
    return (lifetimeOwner.disposed && !lifetimeOwner.settled)
      || typewriterCleanupPending()
      || retiredSends.size > 0
      || retiredSessions.size > 0
      || speechOperations.size > 0
      || !ttsReleases.settled
      || pendingTeardownFailures.length > 0
  }

  async function runCleanup(attempt: ResourceReleaseAttempt) {
    const failures = pendingTeardownFailures.splice(0)
    let lifetimeAttempted = false
    const attemptedTypewriters = new Set<TypewriterOwnership>()
    const observedSpeech = new Set<SpeechOwnership>()
    const attemptedTtsReleases = new Set<AsyncReleaseAction>()
    const attemptedSends = new Set<SendOwnership>()
    const attemptedSessions = new Set<SessionOwnership>()

    for (;;) {
      if (lifetimeOwner.disposed && !lifetimeOwner.settled && !lifetimeAttempted) {
        lifetimeAttempted = true
        try {
          releaseLifetimeResources(attempt)
        } catch (error) {
          failures.push(new Error(`pagehide listener: ${errorMessage(error)}`, { cause: error }))
        }
      }

      const typewriter = typewriterCleanupPending()
        ? typewriterOwnership
        : null
      if (typewriter && !attemptedTypewriters.has(typewriter)) {
        attemptedTypewriters.add(typewriter)
        try {
          releaseTypewriter(typewriter, attempt)
        } catch (error) {
          failures.push(new Error(`typewriter timer: ${errorMessage(error)}`, { cause: error }))
        }
      }

      const speech = [...speechOperations]
        .filter(owner => !observedSpeech.has(owner))
      for (const owner of speech) observedSpeech.add(owner)

      const ttsActions = ttsReleases.actions
        .filter(action => !attemptedTtsReleases.has(action))
      for (const action of ttsActions) attemptedTtsReleases.add(action)

      const concurrentOperations: Promise<unknown>[] = speech
        .map(owner => owner.operation)
      if (ttsActions.length > 0) {
        concurrentOperations.push(ttsReleases.settle(attempt, ttsActions))
      }

      const concurrentResults = await Promise.allSettled(concurrentOperations)
      for (const result of concurrentResults) {
        if (result.status === "rejected") failures.push(result.reason)
      }

      const sends = [...retiredSends]
        .filter(turn => !attemptedSends.has(turn))
      for (const turn of sends) attemptedSends.add(turn)
      try {
        await settleRetiredSends(attempt, sends)
      } catch (error) {
        failures.push(error)
      }

      const sessions = [...retiredSessions]
        .filter(owner => !attemptedSessions.has(owner))
      for (const owner of sessions) attemptedSessions.add(owner)
      try {
        await settleRetiredSessions(attempt, sessions)
      } catch (error) {
        failures.push(error)
      }

      // Awaited cleanup can publish additional exact resources, such as scope
      // disposal adding the lifetime listener while a pagehide release is in
      // flight. Drain those newly published claims in another pass, but never
      // replay a resource already attempted with this exact identity.
      failures.push(...pendingTeardownFailures.splice(0))
      if (failures.length > 0) {
        throw new AggregateError(failures, "PiP chat teardown failed")
      }
      if (!hasPendingCleanupResources()) return

      const hasUnattemptedResources =
        (lifetimeOwner.disposed && !lifetimeOwner.settled && !lifetimeAttempted)
        || (typewriterCleanupPending()
          && typewriterOwnership !== null
          && !attemptedTypewriters.has(typewriterOwnership))
        || [...speechOperations].some(owner => !observedSpeech.has(owner))
        || ttsReleases.actions.some(action => !attemptedTtsReleases.has(action))
        || [...retiredSends].some(turn => !attemptedSends.has(turn))
        || [...retiredSessions].some(owner => !attemptedSessions.has(owner))

      if (!hasUnattemptedResources) {
        throw new AggregateError(
          [new Error("PiP chat teardown resources remain unresolved")],
          "PiP chat teardown failed",
        )
      }
    }
  }

  function startCleanupOperation(attempt: ResourceReleaseAttempt = {}): Promise<void> {
    if (cleanupOperation) return cleanupOperation
    if (!cleanupRequested) return RESOLVED_VOID

    cleanupFailed = false
    cleanupFailure = undefined
    const physicalOperation = Promise.resolve().then(() => runCleanup(attempt))
    let operation!: Promise<void>
    operation = physicalOperation
      .then(
        () => {
          cleanupRequested = false
        },
        (error) => {
          cleanupFailed = true
          cleanupFailure = error
          cleanupRequested = true
          reportLifecycleFailure("pip_chat_cleanup_error", error)
        },
      )
      .finally(() => {
        if (cleanupOperation === operation) cleanupOperation = null
      })
    cleanupOperation = operation
    return operation
  }

  function requestEndSession(attempt: ResourceReleaseAttempt = {}) {
    if (lifecycleActive) {
      lifecycleActive = false
      sessionGeneration++
      cleanupRequested = true
    }

    const send = activeSend
    if (send) requestSendRetirement(send, attempt)
    isSending.value = false

    try {
      invalidateSpeechContinuation(attempt)
    } catch (error) {
      cleanupRequested = true
      pendingTeardownFailures.push(error)
      reportLifecycleFailure("pip_chat_typewriter_cleanup_error", error)
    }
    displayedAgentText.value = ""
    showChat.value = false
    userInput.value = ""
    messageWriter.clear()

    const owner = sessionOwnership
    if (owner) requestSessionRetirement(owner, attempt)

    if (
      cleanupRequested
      || retiredSends.size > 0
      || retiredSessions.size > 0
      || speechOperations.size > 0
      || typewriterCleanupPending()
      || (lifetimeOwner.disposed && !lifetimeOwner.settled)
    ) {
      cleanupRequested = true
      startCleanupOperation(attempt)
    }
  }

  async function endSession(): Promise<void> {
    requestEndSession()
    const operation = cleanupOperation
    if (operation) await operation
    if (cleanupFailed) throw cleanupFailure
  }

  function handlePageHide(event: PageTransitionEvent) {
    if (event.persisted) return
    requestEndSession()
  }

  observeAsynchronousScopeTeardown(
    "PiP chat asynchronous teardown",
    async (attempt) => {
      closeLifetimeOwner(attempt)
      cleanupRequested = true
      requestEndSession(attempt)
      const operation = cleanupOperation
      if (operation) await operation
      if (cleanupFailed) throw cleanupFailure
    },
    error => reportLifecycleFailure("pip_chat_cleanup_error", error),
  )

  const setupAttempt: ResourceReleaseAttempt = {}
  try {
    lifetimeOwner.acquire(
      () => window.addEventListener("pagehide", handlePageHide),
      () => window.removeEventListener("pagehide", handlePageHide),
      "PiP chat pagehide listener",
      setupAttempt,
    )
  } catch (error) {
    closeLifetimeOwner(setupAttempt)
    cleanupRequested = true
    if (error instanceof ResourceAcquisitionRollbackError) {
      pendingTeardownFailures.push(error)
    }
    requestEndSession(setupAttempt)
    throw error
  }

  return {
    messages,
    messageWriter,
    userInput,
    isSending,
    showChat,
    displayedAgentText,
    clearDisplayedText,
    ttsState,
    mouthAmplitude,
    ensureSession,
    sendMessage,
    handleKeydown,
    endSession,
  }
}
