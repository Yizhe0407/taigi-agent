import { readonly, ref } from "vue"

import {
  createComponentFailureReporter,
  observeAsynchronousScopeTeardown,
} from "@/lib/component-lifecycle"
import { reportClientEvent } from "@/lib/report-client-event"
import {
  createResourceOwner,
  ResourceAcquisitionRollbackError,
  ResourceOwnerClosedError,
  type AcquiredResource,
  type ResourceClaim,
  type ResourceOwner,
  type ResourceReleaseAttempt,
} from "@/lib/resource-owner"
import {
  createOwnedAnimationFrame,
  createOwnedTimeout,
  type TimerLease,
} from "@/lib/timer-owner"

import { synthesizeSpeech } from "../api/chat"

export type TtsState = "idle" | "loading" | "playing"

const TTS_REQUEST_DEADLINE_MS = 50_000
const TTS_METADATA_DEADLINE_MS = 3_000
const NOISE_FLOOR = 0.22

type TtsPhase = "audio_context" | "request" | "metadata" | "playback"

type TurnResult = {
  durationMs: number | null
  playbackStarted: boolean
}

type TtsTurn = {
  resources: ResourceOwner
  failureReported: boolean
  workOperation: Promise<TurnResult> | null
  workSettled: boolean
  teardownObservedWork: boolean
  physicalOperations: Set<Promise<unknown>>
  releaseAttempt: ResourceReleaseAttempt
}

type AudioContextOwner = {
  gate: ResourceOwner
  context: AudioContext | null
  closeOperation: Promise<void> | null
  closeFailed: boolean
  closeFailure: unknown
  lastCloseAttempt: ResourceReleaseAttempt | null
}

type AudioContextAccess = {
  readonly context: AudioContext
  readonly state: AudioContextState
}

type ObservedTeardownReportingFailure = {
  readonly teardownFailure: unknown
  readonly deliveryFailure: unknown
}

type TeardownAttempt = {
  operation: Promise<void>
  releaseAttempt: ResourceReleaseAttempt
  attemptedTurns: Set<TtsTurn>
  blockedTurns: Set<TtsTurn>
  observedPhysicalOperations: Set<Promise<unknown>>
  attemptedContexts: Set<AudioContextOwner>
  failures: unknown[]
  failureIdentities: Set<unknown>
}

type TeardownOptions = {
  closeAudioContext?: boolean
  inheritedFailures?: readonly unknown[]
  retireCurrent?: boolean
  targetTurn?: TtsTurn
  reason?: unknown
  releaseAttempt?: ResourceReleaseAttempt
}

class TtsCleanupError extends AggregateError {
  constructor(errors: Iterable<unknown>, message: string) {
    super(errors, message)
    this.name = "TtsCleanupError"
  }
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

function isTimeoutError(error: unknown): boolean {
  return error instanceof Error && error.name === "TimeoutError"
}

function cleanupFailure(resource: string, error: unknown): Error {
  return new Error(`${resource}: ${errorMessage(error)}`, { cause: error })
}

function isCleanupContractFailure(error: unknown): boolean {
  return error instanceof TtsCleanupError
    || error instanceof ResourceAcquisitionRollbackError
}

export function useTts() {
  const ttsState = ref<TtsState>("idle")
  const mouthAmplitude = ref(0)
  const lifetime = createResourceOwner("TTS composable")
  const reportObservedTeardownFailure = createComponentFailureReporter(
    "TTS asynchronous teardown",
  )

  let currentTurn: TtsTurn | null = null
  let pendingSpeak: object | null = null
  const retiredTurns = new Set<TtsTurn>()
  let audioContextOwner: AudioContextOwner | null = null
  let teardownAttempt: TeardownAttempt | null = null
  const observedTeardowns = new Map<Promise<void>, Promise<void>>()
  const retainedObservedTeardownReportingFailures =
    new Set<ObservedTeardownReportingFailure>()

  function reportTurnFailure(turn: TtsTurn, type: string, error: unknown): void {
    if (turn.failureReported) return
    turn.failureReported = true
    try {
      reportClientEvent(type, errorMessage(error))
    } catch {
      // Telemetry is best effort; operation and cleanup contracts stay authoritative.
    }
  }

  function reportCleanupError(error: unknown): void {
    if (error instanceof AggregateError) {
      for (const cause of error.errors) reportCleanupError(cause)
      return
    }
    try {
      reportClientEvent("tts_cleanup_error", errorMessage(error))
    } catch {
      // The rejected teardown operation remains the authoritative error path.
    }
  }

  function observeTeardown(operation: Promise<void>): void {
    if (observedTeardowns.has(operation)) return

    let observation!: Promise<void>
    observation = operation.then(
      () => undefined,
      (teardownFailure) => {
        try {
          reportObservedTeardownFailure(teardownFailure)
        } catch (reportingFailure) {
          retainedObservedTeardownReportingFailures.add({
            teardownFailure,
            deliveryFailure: new AggregateError(
              [teardownFailure, reportingFailure],
              "TTS teardown and its failure reporter both failed",
            ),
          })
        }
      },
    ).then(() => {
      if (observedTeardowns.get(operation) === observation) {
        observedTeardowns.delete(operation)
      }
    })
    observedTeardowns.set(operation, observation)
  }

  async function joinObservedTeardowns(): Promise<void> {
    while (observedTeardowns.size > 0) {
      await Promise.all([...observedTeardowns.values()])
    }
  }

  function consumeObservedTeardownReportingFailures():
    ObservedTeardownReportingFailure[] {
    const failures = [...retainedObservedTeardownReportingFailures]
    for (const failure of failures) {
      retainedObservedTeardownReportingFailures.delete(failure)
    }
    return failures
  }

  function addAttemptFailure(attempt: TeardownAttempt, failure: unknown): void {
    if (attempt.failureIdentities.has(failure)) return
    attempt.failureIdentities.add(failure)
    attempt.failures.push(failure)
  }

  function claimTurnResource(
    turn: TtsTurn,
    resource: string,
    release: () => void,
  ): ResourceClaim {
    return turn.resources.claim(resource, () => {
      try {
        release()
      } catch (error) {
        throw cleanupFailure(resource, error)
      }
    })
  }

  function acquireTurnResource<T>(
    turn: TtsTurn,
    resource: string,
    acquire: () => T,
    release: (value: T | undefined) => void,
  ): AcquiredResource<T> {
    return turn.resources.acquire(acquire, (value) => {
      try {
        release(value)
      } catch (error) {
        throw cleanupFailure(resource, error)
      }
    }, resource, turn.releaseAttempt)
  }

  function trackPhysicalOperation<T>(turn: TtsTurn, promise: Promise<T>): Promise<T> {
    let operation!: Promise<T>
    operation = Promise.resolve(promise).then(
      (value) => {
        turn.physicalOperations.delete(operation)
        return value
      },
      (failure) => {
        turn.physicalOperations.delete(operation)
        throw failure
      },
    )
    turn.physicalOperations.add(operation)
    return operation
  }

  function isCurrent(turn: TtsTurn): boolean {
    return currentTurn === turn
      && !turn.resources.disposed
      && !lifetime.disposed
  }

  function assertCurrent(turn: TtsTurn): void {
    if (isCurrent(turn)) return
    if (turn.resources.signal.aborted) throw turn.resources.signal.reason
    throw new ResourceOwnerClosedError("TTS turn is no longer current")
  }

  function releaseClaims(
    turn: TtsTurn,
    claims: readonly (ResourceClaim | null)[],
  ): unknown[] {
    const failures: unknown[] = []
    for (const claim of claims) {
      if (!claim?.active) continue
      try {
        claim.release(turn.releaseAttempt)
      } catch (error) {
        if (error instanceof AggregateError) failures.push(...error.errors)
        else failures.push(error)
      }
    }
    return failures
  }

  function raceWithAbort<T>(turn: TtsTurn, physical: Promise<T>): Promise<T> {
    const signal = turn.resources.signal
    if (signal.aborted) return Promise.reject(signal.reason)

    return new Promise<T>((resolve, reject) => {
      let outcome:
        | { status: "fulfilled"; value: T }
        | { status: "rejected"; reason: unknown }
        | null = null
      let registrationPending = true
      let completed = false
      let abortClaim: ResourceClaim | null = null

      const complete = () => {
        if (completed || outcome === null || registrationPending) return
        completed = true

        if (!turn.resources.disposed) {
          const failures = releaseClaims(turn, [abortClaim])
          if (failures.length > 0) {
            reject(new TtsCleanupError(
              outcome.status === "rejected"
                ? [outcome.reason, ...failures]
                : failures,
              "TTS abort race cleanup failed",
            ))
            return
          }
        }

        if (outcome.status === "fulfilled") resolve(outcome.value)
        else reject(outcome.reason)
      }

      const finish = (
        next:
          | { status: "fulfilled"; value: T }
          | { status: "rejected"; reason: unknown },
      ) => {
        if (outcome !== null) return
        outcome = next
        complete()
      }

      const onAbort = () => finish({ status: "rejected", reason: signal.reason })
      physical.then(
        value => finish({ status: "fulfilled", value }),
        reason => finish({ status: "rejected", reason }),
      )

      try {
        abortClaim = acquireTurnResource(
          turn,
          "operation abort listener",
          () => signal.addEventListener("abort", onAbort),
          () => signal.removeEventListener("abort", onAbort),
        )
      } catch (error) {
        if (outcome === null || !(error instanceof ResourceOwnerClosedError)) {
          outcome = { status: "rejected", reason: error }
        }
      } finally {
        registrationPending = false
      }

      if (signal.aborted) onAbort()
      complete()
    })
  }

  function createAudioElementRelease(audio: HTMLAudioElement): () => void {
    const pending = new Set<"pause" | "source" | "reload">([
      "pause",
      "source",
      "reload",
    ])

    return () => {
      const failures: unknown[] = []
      for (const step of [...pending]) {
        try {
          if (step === "pause") audio.pause()
          else if (step === "source") audio.removeAttribute("src")
          else audio.load()
          pending.delete(step)
        } catch (error) {
          failures.push(cleanupFailure(`audio ${step}`, error))
        }
      }
      if (failures.length > 0) {
        throw new TtsCleanupError(failures, "TTS audio element cleanup failed")
      }
    }
  }

  function acquireAudioElement(turn: TtsTurn, url: string): HTMLAudioElement {
    let rollbackRelease: (() => void) | null = null
    const acquisition = acquireTurnResource(
      turn,
      "audio element construction",
      () => new Audio(url),
      (audio) => {
        if (!audio) return
        rollbackRelease ??= createAudioElementRelease(audio)
        rollbackRelease()
      },
    )
    const audio = acquisition.value

    // Publish granular ownership before retiring the construction transaction.
    claimTurnResource(turn, "audio reload", () => audio.load())
    claimTurnResource(turn, "audio source", () => audio.removeAttribute("src"))
    claimTurnResource(turn, "audio pause", () => audio.pause())
    acquisition.transfer()
    return audio
  }

  function waitForMetadata(turn: TtsTurn, audio: HTMLAudioElement): Promise<number> {
    const signal = turn.resources.signal
    if (signal.aborted) return Promise.reject(signal.reason)

    return new Promise<number>((resolve, reject) => {
      let outcome:
        | { status: "fulfilled"; value: number }
        | { status: "rejected"; reason: unknown }
        | null = null
      let registrationPending = true
      let completed = false
      let loadedClaim: ResourceClaim | null = null
      let errorClaim: ResourceClaim | null = null
      let abortClaim: ResourceClaim | null = null
      let deadline: TimerLease | null = null

      const complete = () => {
        if (completed || outcome === null || registrationPending) return
        completed = true

        if (!turn.resources.disposed) {
          const failures: unknown[] = []
          if (deadline?.active) {
            try {
              deadline.cancel(turn.releaseAttempt)
            } catch (error) {
              failures.push(cleanupFailure("metadata deadline", error))
            }
          }
          failures.push(...releaseClaims(turn, [abortClaim, errorClaim, loadedClaim]))
          if (failures.length > 0) {
            reject(new TtsCleanupError(
              outcome.status === "rejected"
                ? [outcome.reason, ...failures]
                : failures,
              "TTS metadata cleanup failed",
            ))
            return
          }
        }

        if (outcome.status === "fulfilled") resolve(outcome.value)
        else reject(outcome.reason)
      }

      const finish = (
        next:
          | { status: "fulfilled"; value: number }
          | { status: "rejected"; reason: unknown },
      ) => {
        if (outcome !== null) return
        outcome = next
        complete()
      }

      const onLoadedMetadata = () => {
        try {
          const durationMs = audio.duration * 1000
          if (Number.isFinite(durationMs) && durationMs > 0) {
            finish({ status: "fulfilled", value: durationMs })
          } else {
            finish({
              status: "rejected",
              reason: new Error("TTS audio metadata has no valid duration"),
            })
          }
        } catch (error) {
          finish({ status: "rejected", reason: error })
        }
      }
      const onMetadataError = () => finish({
        status: "rejected",
        reason: new Error("TTS audio metadata failed"),
      })
      const onAbort = () => finish({ status: "rejected", reason: signal.reason })
      const onTimeout = (attempt: ResourceReleaseAttempt) => {
        turn.releaseAttempt = attempt
        finish({
          status: "rejected",
          reason: new DOMException("TTS metadata timed out", "TimeoutError"),
        })
      }

      try {
        loadedClaim = acquireTurnResource(
          turn,
          "metadata loaded callback",
          () => { audio.onloadedmetadata = onLoadedMetadata },
          () => {
            if (audio.onloadedmetadata === onLoadedMetadata) audio.onloadedmetadata = null
          },
        )
        if (outcome === null && !turn.resources.disposed) {
          errorClaim = acquireTurnResource(
            turn,
            "metadata error callback",
            () => { audio.onerror = onMetadataError },
            () => {
              if (audio.onerror === onMetadataError) audio.onerror = null
            },
          )
        }
        if (outcome === null && !turn.resources.disposed) {
          abortClaim = acquireTurnResource(
            turn,
            "metadata abort listener",
            () => signal.addEventListener("abort", onAbort),
            () => signal.removeEventListener("abort", onAbort),
          )
        }
        if (outcome === null && !turn.resources.disposed) {
          deadline = createOwnedTimeout(
            turn.resources,
            "metadata deadline",
            onTimeout,
            TTS_METADATA_DEADLINE_MS,
            turn.releaseAttempt,
          )
        }
      } catch (error) {
        if (outcome === null || !(error instanceof ResourceOwnerClosedError)) {
          outcome = { status: "rejected", reason: error }
        }
      } finally {
        registrationPending = false
      }

      if (signal.aborted) onAbort()
      complete()
    })
  }

  function installPlaybackCallbacks(turn: TtsTurn, audio: HTMLAudioElement): void {
    const onEnded = () => finishPlayback(turn)
    const onPlaybackError = () => {
      reportTurnFailure(turn, "tts_playback_error", new Error("TTS audio playback failed"))
      finishPlayback(turn)
    }

    acquireTurnResource(
      turn,
      "playback ended callback",
      () => { audio.onended = onEnded },
      () => {
        if (audio.onended === onEnded) audio.onended = null
      },
    )
    assertCurrent(turn)
    acquireTurnResource(
      turn,
      "playback error callback",
      () => { audio.onerror = onPlaybackError },
      () => {
        if (audio.onerror === onPlaybackError) audio.onerror = null
      },
    )
  }

  function startAmplitudeSampling(
    turn: TtsTurn,
    analyser: AnalyserNode,
    lowBin: number,
    highBin: number,
  ): void {
    const data = new Uint8Array(analyser.frequencyBinCount)
    const binCount = highBin - lowBin + 1

    const scheduleNext = (acquisitionAttempt: ResourceReleaseAttempt) => {
      createOwnedAnimationFrame(turn.resources, "TTS amplitude frame", (_timestamp, attempt) => {
        turn.releaseAttempt = attempt
        if (!isCurrent(turn)) return
        try {
          analyser.getByteFrequencyData(data)
          let sum = 0
          for (let index = lowBin; index <= highBin; index += 1) sum += data[index]!
          const average = sum / (binCount * 255)
          mouthAmplitude.value = average < NOISE_FLOOR
            ? 0
            : Math.min(1, (average - NOISE_FLOOR) * 3.5)
          if (isCurrent(turn)) scheduleNext(attempt)
        } catch (error) {
          if (isCurrent(turn)) reportTurnFailure(turn, "tts_playback_error", error)
          finishPlayback(turn, isCleanupContractFailure(error) ? [error] : [])
        }
      }, acquisitionAttempt)
    }

    scheduleNext(turn.releaseAttempt)
  }

  function retireAudioContextOwnerIfSettled(owner: AudioContextOwner): boolean {
    if (
      owner.context !== null
      || owner.closeOperation !== null
      || !owner.gate.settled
    ) {
      return false
    }
    if (audioContextOwner === owner) audioContextOwner = null
    return true
  }

  function releaseAudioContextGate(
    owner: AudioContextOwner,
    attempt: ResourceReleaseAttempt,
  ): unknown[] {
    const failures: unknown[] = []
    try {
      owner.gate.close(undefined, attempt)
    } catch (error) {
      failures.push(cleanupFailure("audio context close gate", error))
    }
    try {
      owner.gate.dispose(attempt)
    } catch (error) {
      failures.push(cleanupFailure("audio context acquisition", error))
    }
    return failures
  }

  function startAudioContextClose(
    owner: AudioContextOwner,
    attempt: ResourceReleaseAttempt,
  ): Promise<void> | null {
    if (owner.closeOperation) return owner.closeOperation
    const context = owner.context
    if (!context || owner.lastCloseAttempt === attempt) return null

    owner.lastCloseAttempt = attempt
    owner.closeFailed = false
    owner.closeFailure = undefined

    // Publish the authoritative operation before touching the host object. The
    // returned operation always fulfils; exact failure remains owned by the
    // AudioContext owner until a later release attempt succeeds.
    const physicalOperation = Promise.resolve().then(async () => {
      let state: AudioContextState
      try {
        state = context.state
      } catch (error) {
        owner.closeFailed = true
        owner.closeFailure = cleanupFailure("audio context state", error)
        return
      }

      if (state === "closed") {
        if (owner.context === context) owner.context = null
        return
      }

      try {
        await context.close()
      } catch (error) {
        owner.closeFailed = true
        owner.closeFailure = cleanupFailure("audio context", error)
        return
      }
      if (owner.context === context) owner.context = null
    })

    let operation!: Promise<void>
    operation = physicalOperation.then(
      () => {
        if (owner.closeOperation === operation) owner.closeOperation = null
      },
      (unexpectedFailure) => {
        owner.closeFailed = true
        owner.closeFailure = cleanupFailure("audio context", unexpectedFailure)
        if (owner.closeOperation === operation) owner.closeOperation = null
      },
    )
    owner.closeOperation = operation
    return operation
  }

  function requestAudioContextClose(attempt: TeardownAttempt): void {
    const owner = audioContextOwner
    if (!owner) return

    for (const failure of releaseAudioContextGate(owner, attempt.releaseAttempt)) {
      addAttemptFailure(attempt, failure)
    }
    startAudioContextClose(owner, attempt.releaseAttempt)
    retireAudioContextOwnerIfSettled(owner)
  }

  function createAudioContext(
    releaseAttempt: ResourceReleaseAttempt,
  ): AudioContext {
    const owner: AudioContextOwner = {
      gate: createResourceOwner("TTS AudioContext construction"),
      context: null,
      closeOperation: null,
      closeFailed: false,
      closeFailure: undefined,
      lastCloseAttempt: null,
    }
    audioContextOwner = owner

    try {
      const acquisition = owner.gate.acquire(
        () => new AudioContext(),
        (context, cleanupAttempt = releaseAttempt) => {
          if (!context) return
          if (!owner.context) owner.context = context
          startAudioContextClose(owner, cleanupAttempt)
        },
        "AudioContext construction",
        releaseAttempt,
      )
      owner.context = acquisition.value
      acquisition.transfer()
      return acquisition.value
    } catch (setupError) {
      const cleanupFailures = releaseAudioContextGate(owner, releaseAttempt)
      startAudioContextClose(owner, releaseAttempt)
      retireAudioContextOwnerIfSettled(owner)

      if (cleanupFailures.length > 0) {
        throw new TtsCleanupError(
          [setupError, ...cleanupFailures],
          "AudioContext construction and rollback failed",
        )
      }
      throw setupError
    }
  }

  function getAudioContext(
    releaseAttempt: ResourceReleaseAttempt,
  ): AudioContextAccess {
    if (!audioContextOwner) createAudioContext(releaseAttempt)
    const owner = audioContextOwner
    if (!owner) throw new Error("TTS AudioContext ownership was not published")
    if (owner.gate.disposed) {
      throw new Error("Previous TTS AudioContext cleanup has not settled")
    }

    const context = owner.context
    if (!context) throw new Error("TTS AudioContext ownership is incomplete")

    let state: AudioContextState
    try {
      state = context.state
    } catch (error) {
      const stateFailure = cleanupFailure("audio context state", error)
      const cleanupFailures = releaseAudioContextGate(owner, releaseAttempt)
      owner.lastCloseAttempt = releaseAttempt
      owner.closeFailed = true
      owner.closeFailure = cleanupFailures.length === 0
        ? stateFailure
        : new TtsCleanupError(
            [stateFailure, ...cleanupFailures],
            "AudioContext state read and rollback failed",
          )
      retireAudioContextOwnerIfSettled(owner)
      if (cleanupFailures.length > 0) throw owner.closeFailure
      throw error
    }
    if (state !== "closed") return { context, state }

    const cleanupFailures = releaseAudioContextGate(owner, releaseAttempt)
    owner.context = null
    owner.lastCloseAttempt = releaseAttempt
    owner.closeFailed = false
    owner.closeFailure = undefined
    retireAudioContextOwnerIfSettled(owner)
    if (cleanupFailures.length > 0) {
      throw new TtsCleanupError(
        cleanupFailures,
        "Closed AudioContext ownership cleanup failed",
      )
    }
    createAudioContext(releaseAttempt)
    return getAudioContext(releaseAttempt)
  }

  function retireTurn(
    turn: TtsTurn,
    attempt: TeardownAttempt,
    reason?: unknown,
  ): void {
    if (currentTurn === turn) {
      currentTurn = null
      ttsState.value = "idle"
      mouthAmplitude.value = 0
    }
    retiredTurns.add(turn)
    turn.releaseAttempt = attempt.releaseAttempt
    if (turn.resources.disposed) return

    try {
      turn.resources.close(reason, attempt.releaseAttempt)
    } catch (error) {
      addAttemptFailure(attempt, cleanupFailure("TTS turn close gate", error))
    }
  }

  function drainTurnDisposals(attempt: TeardownAttempt): void {
    for (const turn of retiredTurns) {
      if (attempt.attemptedTurns.has(turn)) continue
      attempt.attemptedTurns.add(turn)
      turn.releaseAttempt = attempt.releaseAttempt

      try {
        turn.resources.dispose(turn.releaseAttempt)
      } catch (error) {
        addAttemptFailure(attempt, error)
      }
    }
  }

  async function settleAudioContext(attempt: TeardownAttempt): Promise<boolean> {
    const owner = audioContextOwner
    if (!owner || !owner.gate.disposed || attempt.attemptedContexts.has(owner)) {
      return false
    }
    attempt.attemptedContexts.add(owner)

    for (const failure of releaseAudioContextGate(owner, attempt.releaseAttempt)) {
      addAttemptFailure(attempt, failure)
    }

    const olderOperation = owner.closeOperation
    if (olderOperation) await olderOperation

    const operation = startAudioContextClose(owner, attempt.releaseAttempt)
    if (operation) await operation
    if (owner.closeFailed) {
      addAttemptFailure(attempt, owner.closeFailure)
    }

    retireAudioContextOwnerIfSettled(owner)
    return true
  }

  function pruneSettledTurns(): void {
    for (const turn of retiredTurns) {
      if (
        turn.resources.settled
        && turn.physicalOperations.size === 0
        && turn.workOperation !== null
      ) {
        retiredTurns.delete(turn)
      }
    }
  }

  async function runTeardownAttempt(attempt: TeardownAttempt): Promise<void> {
    for (;;) {
      drainTurnDisposals(attempt)

      // A failed AbortController release can leave the signal live. Joining work
      // in that state would make teardown depend on an operation it failed to
      // cancel, so retain the turn and fail this exact attempt immediately.
      for (const turn of retiredTurns) {
        if (
          turn.workOperation === null
          || turn.workSettled
          || turn.resources.signal.aborted
          || attempt.blockedTurns.has(turn)
        ) continue

        attempt.blockedTurns.add(turn)
        addAttemptFailure(
          attempt,
          new Error("TTS turn cancellation debt remains unresolved"),
        )
      }

      const work = [...retiredTurns].filter(turn => (
        turn.workOperation !== null
        && !turn.teardownObservedWork
        && !attempt.blockedTurns.has(turn)
      ))
      if (work.length > 0) {
        const results = await Promise.allSettled(
          work.map(turn => turn.workOperation as Promise<TurnResult>),
        )
        for (let index = 0; index < results.length; index += 1) {
          const turn = work[index]!
          const result = results[index]!
          turn.teardownObservedWork = true
          if (result.status === "rejected") addAttemptFailure(attempt, result.reason)
        }
        continue
      }

      const physicalOperations = [...retiredTurns]
        .filter(turn => !attempt.blockedTurns.has(turn))
        .flatMap(turn => [...turn.physicalOperations])
        .filter(operation => !attempt.observedPhysicalOperations.has(operation))
      if (physicalOperations.length > 0) {
        for (const operation of physicalOperations) {
          attempt.observedPhysicalOperations.add(operation)
        }
        await Promise.allSettled(physicalOperations)
        continue
      }

      if (await settleAudioContext(attempt)) continue

      drainTurnDisposals(attempt)
      pruneSettledTurns()

      const hasUnobservedWork = [...retiredTurns].some(turn => (
        turn.workOperation !== null
        && !turn.teardownObservedWork
        && !attempt.blockedTurns.has(turn)
      ))
      const hasUnobservedPhysicalOperation = [...retiredTurns].some(turn => (
        !attempt.blockedTurns.has(turn)
        && [...turn.physicalOperations]
          .some(operation => !attempt.observedPhysicalOperations.has(operation))
      ))
      const context = audioContextOwner
      const hasUnattemptedContext = context !== null
        && context.gate.disposed
        && !attempt.attemptedContexts.has(context)
      const hasUnattemptedTurn = [...retiredTurns]
        .some(turn => !attempt.attemptedTurns.has(turn))
      if (
        hasUnobservedWork
        || hasUnobservedPhysicalOperation
        || hasUnattemptedContext
        || hasUnattemptedTurn
      ) {
        continue
      }
      break
    }

    pruneSettledTurns()
    if (attempt.failures.length > 0) {
      const error = new AggregateError(attempt.failures, "TTS teardown failed")
      reportCleanupError(error)
      throw error
    }
  }

  function createTeardownAttempt(
    releaseAttempt: ResourceReleaseAttempt = {},
  ): TeardownAttempt {
    const attempt = {
      operation: Promise.resolve(),
      releaseAttempt,
      attemptedTurns: new Set<TtsTurn>(),
      blockedTurns: new Set<TtsTurn>(),
      observedPhysicalOperations: new Set<Promise<unknown>>(),
      attemptedContexts: new Set<AudioContextOwner>(),
      failures: [],
      failureIdentities: new Set<unknown>(),
    } satisfies TeardownAttempt

    const physicalOperation = Promise.resolve().then(() => runTeardownAttempt(attempt))
    let operation!: Promise<void>
    operation = physicalOperation.finally(() => {
      if (teardownAttempt?.operation === operation) teardownAttempt = null
    })
    attempt.operation = operation
    teardownAttempt = attempt
    return attempt
  }

  function requestTeardown(options: TeardownOptions = {}): Promise<void> {
    const attempt = teardownAttempt ?? createTeardownAttempt(options.releaseAttempt)
    for (const failure of options.inheritedFailures ?? []) {
      addAttemptFailure(attempt, failure)
    }
    if (options.targetTurn) retireTurn(options.targetTurn, attempt, options.reason)
    if (options.retireCurrent && currentTurn) {
      retireTurn(currentTurn, attempt, options.reason)
    }
    if (options.closeAudioContext) requestAudioContextClose(attempt)
    drainTurnDisposals(attempt)
    return attempt.operation
  }

  async function settleTeardown(options: TeardownOptions = {}): Promise<void> {
    // Close the requested gates synchronously, then join every callback-owned
    // observation before consuming its delivery debt. This is the public
    // boundary for both physical cleanup and failures from a broken reporter.
    const operation = requestTeardown(options)
    await joinObservedTeardowns()

    let teardownOutcome: { failed: false } | { failed: true; failure: unknown } = {
      failed: false,
    }
    try {
      await operation
    } catch (failure) {
      teardownOutcome = { failed: true, failure }
    }
    await joinObservedTeardowns()

    const reportingFailures = consumeObservedTeardownReportingFailures()
    const failures = reportingFailures.map(failure => failure.deliveryFailure)
    if (
      teardownOutcome.failed
      && !reportingFailures.some(failure => (
        failure.teardownFailure === teardownOutcome.failure
      ))
    ) {
      failures.push(teardownOutcome.failure)
    }

    if (failures.length === 1) throw failures[0]
    if (failures.length > 1) {
      throw new AggregateError(failures, "TTS observed teardown failed")
    }
  }

  function finishPlayback(
    turn: TtsTurn,
    inheritedFailures: readonly unknown[] = [],
  ): void {
    if (currentTurn !== turn || turn.resources.disposed) return
    const operation = requestTeardown({
      inheritedFailures,
      targetTurn: turn,
      releaseAttempt: turn.releaseAttempt,
    })
    observeTeardown(operation)
  }

  async function runTurn(turn: TtsTurn, text: string): Promise<TurnResult> {
    let phase: TtsPhase = "audio_context"

    try {
      assertCurrent(turn)
      const { context, state: contextState } = getAudioContext(turn.releaseAttempt)
      assertCurrent(turn)

      if (contextState === "suspended") {
        const resume = trackPhysicalOperation(turn, Promise.resolve(context.resume()))
        await raceWithAbort(turn, resume)
      }
      assertCurrent(turn)

      const requestDeadline = createOwnedTimeout(
        turn.resources,
        "TTS request deadline",
        (attempt) => {
          turn.releaseAttempt = attempt
          if (!isCurrent(turn)) return
          turn.resources.close(
            new DOMException("TTS request timed out", "TimeoutError"),
            attempt,
          )
        },
        TTS_REQUEST_DEADLINE_MS,
        turn.releaseAttempt,
      )
      assertCurrent(turn)

      phase = "request"
      const request = trackPhysicalOperation(
        turn,
        synthesizeSpeech(text, turn.resources.signal),
      )
      const blob = await raceWithAbort(turn, request)
      assertCurrent(turn)

      const urlAcquisition = acquireTurnResource(
        turn,
        "audio blob URL",
        () => URL.createObjectURL(blob),
        url => { if (url !== undefined) URL.revokeObjectURL(url) },
      )
      const audio = acquireAudioElement(turn, urlAcquisition.value)
      assertCurrent(turn)

      phase = "metadata"
      const durationMs = await waitForMetadata(turn, audio)
      assertCurrent(turn)

      phase = "playback"
      let sourceCleanupTransferred = false
      const sourceAcquisition = acquireTurnResource(
        turn,
        "audio source node construction",
        () => context.createMediaElementSource(audio),
        source => {
          if (source && !sourceCleanupTransferred) source.disconnect()
        },
      )
      const source = sourceAcquisition.value

      let analyserCleanupTransferred = false
      const analyserAcquisition = acquireTurnResource(
        turn,
        "audio analyser construction",
        () => context.createAnalyser(),
        analyser => {
          if (analyser && !analyserCleanupTransferred) analyser.disconnect()
        },
      )
      const analyser = analyserAcquisition.value

      analyser.fftSize = 512
      assertCurrent(turn)
      analyser.smoothingTimeConstant = 0.2
      assertCurrent(turn)
      analyser.minDecibels = -70
      assertCurrent(turn)
      analyser.maxDecibels = -20
      assertCurrent(turn)

      sourceCleanupTransferred = true
      acquireTurnResource(
        turn,
        "audio source connection",
        () => source.connect(analyser),
        () => source.disconnect(analyser),
      )
      sourceAcquisition.transfer()
      assertCurrent(turn)

      analyserCleanupTransferred = true
      acquireTurnResource(
        turn,
        "audio analyser connection",
        () => analyser.connect(context.destination),
        () => analyser.disconnect(context.destination),
      )
      analyserAcquisition.transfer()
      assertCurrent(turn)

      const binHz = context.sampleRate / analyser.fftSize
      const lowBin = Math.max(0, Math.round(300 / binHz))
      const highBin = Math.min(
        analyser.frequencyBinCount - 1,
        Math.round(3400 / binHz),
      )

      installPlaybackCallbacks(turn, audio)
      assertCurrent(turn)
      const playback = trackPhysicalOperation(turn, Promise.resolve(audio.play()))
      await raceWithAbort(turn, playback)
      assertCurrent(turn)

      try {
        requestDeadline.cancel(turn.releaseAttempt)
      } catch (error) {
        throw new TtsCleanupError(
          [cleanupFailure("TTS request deadline", error)],
          "TTS request deadline cleanup failed",
        )
      }

      ttsState.value = "playing"
      startAmplitudeSampling(turn, analyser, lowBin, highBin)
      return { durationMs, playbackStarted: true }
    } catch (error) {
      if (isCleanupContractFailure(error)) throw error

      const deadlineError = isTimeoutError(turn.resources.signal.reason)
      const expectedCancellation = lifetime.disposed
        || currentTurn !== turn
        || (turn.resources.signal.aborted && !deadlineError)
      if (!expectedCancellation) {
        const failure = deadlineError ? turn.resources.signal.reason : error
        const type = deadlineError ? "tts_request_timeout" : `tts_${phase}_error`
        reportTurnFailure(turn, type, failure)
      }
      return { durationMs: null, playbackStarted: false }
    }
  }

  // Returns audio duration in ms once playback starts, null if aborted/failed.
  async function speak(text: string): Promise<number | null> {
    if (lifetime.disposed) return null

    const request = {}
    pendingSpeak = request
    try {
      await settleTeardown({ retireCurrent: true })
    } catch (error) {
      if (pendingSpeak === request) pendingSpeak = null
      throw error
    }
    if (pendingSpeak !== request || lifetime.disposed) return null

    const turn: TtsTurn = {
      resources: createResourceOwner("TTS turn"),
      failureReported: false,
      workOperation: null,
      workSettled: false,
      teardownObservedWork: false,
      physicalOperations: new Set(),
      releaseAttempt: {},
    }
    currentTurn = turn
    pendingSpeak = null
    ttsState.value = "loading"

    const physicalWork = Promise.resolve().then(() => runTurn(turn, text))
    const workOperation = physicalWork.then(
      (result) => {
        turn.workSettled = true
        return result
      },
      (failure) => {
        turn.workSettled = true
        throw failure
      },
    )
    turn.workOperation = workOperation

    let result: TurnResult
    try {
      result = await workOperation
    } catch (error) {
      await settleTeardown({
        targetTurn: turn,
        releaseAttempt: turn.releaseAttempt,
      })
      throw error
    }

    if (result.playbackStarted) return result.durationMs
    await settleTeardown({
      targetTurn: turn,
      releaseAttempt: turn.releaseAttempt,
    })
    return null
  }

  async function release(): Promise<void> {
    pendingSpeak = null
    await settleTeardown({ closeAudioContext: true, retireCurrent: true })
  }

  observeAsynchronousScopeTeardown(
    "TTS asynchronous teardown",
    async (attempt) => {
      pendingSpeak = null
      const failures: unknown[] = []
      try {
        lifetime.dispose(attempt)
      } catch (error) {
        failures.push(cleanupFailure("TTS lifetime gate", error))
      }
      ttsState.value = "idle"
      mouthAmplitude.value = 0
      try {
        await settleTeardown({
          closeAudioContext: true,
          retireCurrent: true,
          releaseAttempt: attempt,
        })
      } catch (error) {
        failures.push(error)
      }
      if (failures.length === 1) throw failures[0]
      if (failures.length > 1) {
        throw new AggregateError(failures, "TTS scope teardown failed")
      }
    },
    reportObservedTeardownFailure,
  )

  return { ttsState, mouthAmplitude: readonly(mouthAmplitude), speak, release }
}
