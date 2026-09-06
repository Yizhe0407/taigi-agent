import { readonly, ref } from "vue"

import { apiBaseUrl, readJsonResponse } from "@/lib/api"
import {
  createAsyncReleaseOwner,
  type AsyncReleaseAction,
  type AsyncReleaseOwner,
} from "@/lib/async-release-owner"
import {
  createComponentFailureReporter,
  observeAsynchronousScopeTeardown,
} from "@/lib/component-lifecycle"
import { reportClientEvent } from "@/lib/report-client-event"
import {
  createResourceOwner,
  ResourceAcquisitionRollbackError,
  ResourceOwnerClosedError,
  type ResourceClaim,
  type ResourceOwner,
  type ResourceReleaseAttempt,
} from "@/lib/resource-owner"
import {
  createOwnedAnimationFrame,
  createOwnedTimeout,
} from "@/lib/timer-owner"

export type WebRTCState = "disconnected" | "connecting" | "connected" | "error"

type IceServersResponse = {
  iceServers: RTCIceServer[]
}

export type WebRTCCallbacks = {
  onTranscript: (text: string) => void
  onReply: (text: string) => void
  onCancelled?: () => void
  onSubtitle?: (text: string, durationMs: number) => void
  onBotSpeaking?: () => void
  onBotSilent?: () => void
  onUserSpeaking?: () => void
  onUserSilent?: () => void
  onEndConversation?: () => void
}

const ICE_GATHERING_TIMEOUT_MS = 10_000

type DataChannelMessage = {
  type: string
  text?: string
  durationMs?: number
}

type CleanupAction = AsyncReleaseAction

type AudioResumeAttempt = {
  context: AudioContext
  retryAttempt: ResourceReleaseAttempt | null
}

type ConnectionSetup = {
  releaseAttempt: ResourceReleaseAttempt
}

type RemoteAudioAttachment = {
  audio: HTMLAudioElement
  stream: MediaStream
  cleanup: CleanupAction
}

type RemoteAmplitudeGraph = {
  stream: MediaStream
  context: AudioContext
  resources: ResourceOwner | null
  source: MediaStreamAudioSourceNode | null
  analyser: AnalyserNode | null
  cleanup: CleanupAction
}

type PeerTransport = {
  resources: ResourceOwner | null
  peer: RTCPeerConnection | null
  channel: RTCDataChannel | null
  pcId: string | null
  readyPending: boolean
}

type PeerTransportSetup = {
  transport: PeerTransport
  resources: ResourceOwner
  finish(): void
}

type RemoteStreamCandidate = {
  stream: MediaStream
  fallbackClaim: ResourceClaim | null
}

type OwnedOperation = {
  promise: Promise<void>
  failed: boolean
  failure: unknown
}

type ConnectionOwner = {
  controller: AbortController | null
  cancellationCleanup: CleanupAction | null
  transactionResources: ResourceOwner | null
  transactionCleanup: CleanupAction | null
  stream: MediaStream | null
  localStreamCleanup: CleanupAction | null
  context: AudioContext | null
  audioContextCleanup: CleanupAction | null
  transport: PeerTransport | null
  remoteStream: MediaStream | null
  remoteAudio: HTMLAudioElement | null
  remoteAttachment: RemoteAudioAttachment | null
  remoteAmplitudeGraph: RemoteAmplitudeGraph | null
  remotePlaybackId: number
  pointerdownRetryHandler: (() => void) | null
  pointerdownRetryCleanup: CleanupAction | null
  resumeAttempt: AudioResumeAttempt | null
  connectOperation: Promise<void> | null
  connectSettled: boolean
  connectTerminalFailed: boolean
  connectTerminalFailure: unknown
  operations: Set<OwnedOperation>
  cleanupOwner: AsyncReleaseOwner
  closing: boolean
  teardownOperation: Promise<void> | null
  teardownAttempt: ResourceReleaseAttempt | null
  teardownFailed: boolean
  teardownFailure: unknown
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

export function useWebRTC(callbacks: WebRTCCallbacks) {
  const state = ref<WebRTCState>("disconnected")
  const mouthAmplitude = ref(0)
  const micDenied = ref(false)
  const reportObservedTeardownFailure = createComponentFailureReporter(
    "WebRTC asynchronous teardown",
  )

  let owner: ConnectionOwner | null = null
  let lifecycleId = 0
  let destroyed = false

  function isOwnedOpen(connection: ConnectionOwner): boolean {
    return owner === connection && !connection.closing && !destroyed
  }

  function isCurrent(connection: ConnectionOwner): boolean {
    const controller = connection.controller
    return isOwnedOpen(connection)
      && controller !== null
      && !controller.signal.aborted
  }

  function reportCleanupError(error: unknown) {
    if (error instanceof AggregateError) {
      for (const cause of error.errors) reportCleanupError(cause)
      return
    }
    reportClientEvent("webrtc_cleanup_error", errorMessage(error))
  }

  function addCleanupAction(
    connection: ConnectionOwner,
    resource: string,
    release: (attempt: ResourceReleaseAttempt) => void | Promise<void>,
  ): CleanupAction {
    return connection.cleanupOwner.claim(resource, release)
  }

  function acquireResourceGroup(
    connection: ConnectionOwner,
    resource: string,
    label: string,
    publish: (action: CleanupAction) => void = () => {},
    released: (action: CleanupAction, resources: ResourceOwner | null) => void = () => {},
  ): ResourceOwner {
    let resources: ResourceOwner | null = null
    let acquisitionPending = true
    let settleAcquisition!: () => void
    const acquisitionSettled = new Promise<void>((resolve) => {
      settleAcquisition = resolve
    })

    let action!: CleanupAction
    action = addCleanupAction(connection, resource, async (attempt) => {
      if (acquisitionPending) await acquisitionSettled
      if (resources) {
        resources.dispose(attempt)
        if (!resources.settled) {
          // ResourceOwner acquisitions are synchronous. A teardown that re-enters
          // the host call must yield until that exact call returns and performs its
          // requested rollback before deciding whether cleanup debt remains.
          await Promise.resolve()
        }
        if (!resources.settled) {
          throw new Error(`${resource} acquisition or release remains unsettled`)
        }
      }
      released(action, resources)
    })
    publish(action)

    try {
      resources = createResourceOwner(label)
      return resources
    } finally {
      acquisitionPending = false
      settleAcquisition()
    }
  }

  function acquireConnectionController(connection: ConnectionOwner): AbortController {
    let acquiredController: AbortController | null = null
    let acquisitionPending = true
    let settleAcquisition!: () => void
    const acquisitionSettled = new Promise<void>((resolve) => {
      settleAcquisition = resolve
    })

    let action!: CleanupAction
    action = addCleanupAction(connection, "connection cancellation", async (attempt) => {
      if (acquisitionPending) await acquisitionSettled
      acquiredController?.abort(attempt)
      if (connection.controller === acquiredController) connection.controller = null
      if (connection.cancellationCleanup === action) connection.cancellationCleanup = null
    })
    connection.cancellationCleanup = action

    try {
      acquiredController = new AbortController()
      if (owner === connection) connection.controller = acquiredController
      return acquiredController
    } finally {
      acquisitionPending = false
      settleAcquisition()
    }
  }

  function acquireTransactionResources(connection: ConnectionOwner): ResourceOwner {
    const resources = acquireResourceGroup(
      connection,
      "transactional browser resources",
      "WebRTC transactional resources",
      action => { connection.transactionCleanup = action },
      (action, releasedResources) => {
        if (connection.transactionCleanup === action) connection.transactionCleanup = null
        if (connection.transactionResources === releasedResources) {
          connection.transactionResources = null
        }
      },
    )
    if (owner === connection) connection.transactionResources = resources
    return resources
  }

  function beginPeerTransportSetup(connection: ConnectionOwner): PeerTransportSetup {
    let setupPending = true
    let settleSetup!: () => void
    const setupSettled = new Promise<void>((resolve) => {
      settleSetup = resolve
    })

    let transport!: PeerTransport
    addCleanupAction(
      connection,
      "peer transport",
      async (attempt) => {
        if (setupPending) await setupSettled
        const resources = transport.resources
        if (resources) {
          resources.dispose(attempt)
          if (!resources.settled) {
            throw new Error("Peer transport cleanup remains unsettled")
          }
        }
        transport.readyPending = false
        if (connection.transport === transport) connection.transport = null
      },
    )
    transport = {
      resources: null,
      peer: null,
      channel: null,
      pcId: null,
      readyPending: false,
    }
    // Publish the one authoritative transport before constructing any host
    // object. Teardown joins this complete synchronous setup section and then
    // releases callbacks, channel, and peer in reverse acquisition order.
    connection.transport = transport

    let finished = false
    try {
      const resources = createResourceOwner("WebRTC peer transport")
      transport.resources = resources
      return {
        transport,
        resources,
        finish() {
          if (finished) return
          finished = true
          setupPending = false
          settleSetup()
        },
      }
    } catch (error) {
      setupPending = false
      settleSetup()
      throw error
    }
  }

  function isCurrentTransport(
    connection: ConnectionOwner,
    transport: PeerTransport,
  ): boolean {
    return isCurrent(connection) && connection.transport === transport
  }

  function createMediaStreamRelease(
    stream: MediaStream,
    resource: string,
  ): () => void {
    let pendingTracks: Set<MediaStreamTrack> | null = null

    return () => {
      if (pendingTracks === null) pendingTracks = new Set(stream.getTracks())

      const failures: unknown[] = []
      for (const track of [...pendingTracks]) {
        try {
          track.stop()
          pendingTracks.delete(track)
        } catch (error) {
          failures.push(error)
        }
      }

      if (failures.length === 1) throw failures[0]
      if (failures.length > 1) {
        throw new AggregateError(failures, `Failed to stop ${resource}`)
      }
    }
  }

  async function acquireLocalStream(
    connection: ConnectionOwner,
  ): Promise<MediaStream> {
    let acquiredStream: MediaStream | null = null
    let streamRelease: (() => void) | null = null
    let settleAcquisition!: () => void
    const acquisitionSettled = new Promise<void>((resolve) => {
      settleAcquisition = resolve
    })

    let action!: CleanupAction
    action = addCleanupAction(connection, "local media stream", async () => {
      await acquisitionSettled
      streamRelease?.()
      if (connection.stream === acquiredStream) connection.stream = null
      if (connection.localStreamCleanup === action) connection.localStreamCleanup = null
    })
    connection.localStreamCleanup = action

    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      })
      acquiredStream = stream
      streamRelease = createMediaStreamRelease(stream, "local media stream tracks")
      if (isCurrent(connection)) connection.stream = stream
      return stream
    } finally {
      settleAcquisition()
    }
  }

  async function acquireAudioContext(
    connection: ConnectionOwner,
  ): Promise<AudioContext> {
    let acquiredContext: AudioContext | null = null
    let settleAcquisition!: () => void
    const acquisitionSettled = new Promise<void>((resolve) => {
      settleAcquisition = resolve
    })

    let action!: CleanupAction
    action = addCleanupAction(connection, "audio context", async () => {
      await acquisitionSettled
      if (acquiredContext) await acquiredContext.close()
      if (connection.context === acquiredContext) connection.context = null
      if (connection.audioContextCleanup === action) connection.audioContextCleanup = null
    })
    connection.audioContextCleanup = action

    try {
      const context = new AudioContext()
      acquiredContext = context
      if (isCurrent(connection)) connection.context = context
      return context
    } finally {
      settleAcquisition()
    }
  }

  function startCleanupAction(
    connection: ConnectionOwner,
    action: CleanupAction,
    attempt: ResourceReleaseAttempt,
  ): Promise<void> {
    return connection.cleanupOwner.start(action, attempt)
  }

  async function settleCleanupActions(
    connection: ConnectionOwner,
    attempt: ResourceReleaseAttempt,
    attempted: Set<CleanupAction>,
    candidates: readonly CleanupAction[] = connection.cleanupOwner.actions,
  ) {
    const ownedActions = new Set(connection.cleanupOwner.actions)
    const actions = [...new Set(
      candidates.filter(action => ownedActions.has(action) && !attempted.has(action)),
    )]
    if (actions.length === 0) return
    for (const action of actions) attempted.add(action)

    // The shared owner publishes every physical release before invoking host
    // cleanup, joins asynchronous completions, and retains only failed exact
    // claims for the next distinct lifecycle attempt.
    await connection.cleanupOwner.settle(attempt, actions)
  }

  async function waitForIceGathering(
    connection: ConnectionOwner,
    peer: RTCPeerConnection,
    setup: ConnectionSetup,
    onTimeout: () => void,
  ): Promise<void> {
    const controller = connection.controller
    if (!controller) return
    const signal = controller.signal
    if (peer.iceGatheringState === "complete" || signal.aborted) return

    let cleanupAction!: CleanupAction
    const resources = acquireResourceGroup(
      connection,
      "ICE gathering resources",
      "WebRTC ICE gathering",
      action => { cleanupAction = action },
    )

    let resolveGathering!: () => void
    const gathering = new Promise<void>((resolve) => {
      resolveGathering = resolve
    })
    const NO_TERMINAL_FAILURE = Symbol("no ICE gathering failure")
    let terminalFailure: unknown | typeof NO_TERMINAL_FAILURE = NO_TERMINAL_FAILURE
    let settled = false
    let releaseAttempt = setup.releaseAttempt

    const finish = (
      attempt: ResourceReleaseAttempt,
      failure: unknown | typeof NO_TERMINAL_FAILURE = NO_TERMINAL_FAILURE,
    ) => {
      if (settled) return
      settled = true
      releaseAttempt = attempt
      setup.releaseAttempt = attempt
      terminalFailure = failure
      resolveGathering()
    }
    const onStateChange = () => {
      if (settled) return
      const callbackAttempt: ResourceReleaseAttempt = {}
      try {
        if (peer.iceGatheringState === "complete") finish(callbackAttempt)
      } catch (error) {
        finish(callbackAttempt, error)
      }
    }
    const onAbort = () => {
      if (settled) return
      const reason = signal.reason
      finish(
        reason !== null && typeof reason === "object"
          ? reason as ResourceReleaseAttempt
          : setup.releaseAttempt,
      )
    }

    try {
      createOwnedTimeout(
        resources,
        "ICE gathering deadline",
        (callbackAttempt) => {
          if (settled) return
          try {
            onTimeout()
            finish(callbackAttempt)
          } catch (error) {
            finish(callbackAttempt, error)
          }
        },
        ICE_GATHERING_TIMEOUT_MS,
        setup.releaseAttempt,
      )

      if (!settled) {
        resources.acquire(
          () => peer.addEventListener("icegatheringstatechange", onStateChange),
          () => peer.removeEventListener("icegatheringstatechange", onStateChange),
          "ICE gathering listener",
          setup.releaseAttempt,
        )
      }
      if (!settled) {
        resources.acquire(
          () => signal.addEventListener("abort", onAbort, { once: true }),
          () => signal.removeEventListener("abort", onAbort),
          "ICE gathering cancellation listener",
          setup.releaseAttempt,
        )
        if (signal.aborted) onAbort()
      }
    } catch (error) {
      finish(setup.releaseAttempt, error)
    }

    await gathering

    // A connection teardown that delivered the abort owns this exact cleanup
    // action and attempt. Join it there instead of creating a competing release.
    const NO_CLEANUP_FAILURE = Symbol("no ICE cleanup failure")
    let iceCleanupFailure: unknown | typeof NO_CLEANUP_FAILURE = NO_CLEANUP_FAILURE
    if (!connection.closing) {
      try {
        await settleCleanupActions(
          connection,
          releaseAttempt,
          new Set(),
          [cleanupAction],
        )
      } catch (error) {
        iceCleanupFailure = error
      } finally {
        setup.releaseAttempt = cleanupAction.lastAttempt ?? releaseAttempt
      }
    }

    if (
      terminalFailure !== NO_TERMINAL_FAILURE
      && iceCleanupFailure !== NO_CLEANUP_FAILURE
    ) {
      throw new AggregateError(
        [terminalFailure, iceCleanupFailure],
        "ICE gathering operation and cleanup failed",
      )
    }
    if (terminalFailure !== NO_TERMINAL_FAILURE) throw terminalFailure
    if (iceCleanupFailure !== NO_CLEANUP_FAILURE) throw iceCleanupFailure
  }

  function abortConnection(
    connection: ConnectionOwner,
    attempt: ResourceReleaseAttempt,
  ) {
    const action = connection.cancellationCleanup
    if (action) startCleanupAction(connection, action, attempt)
  }

  function enqueuePointerdownCleanup(connection: ConnectionOwner): CleanupAction | null {
    return connection.pointerdownRetryCleanup
  }

  function enqueueAmplitudeCleanup(connection: ConnectionOwner): CleanupAction[] {
    const graph = connection.remoteAmplitudeGraph
    if (!destroyed) mouthAmplitude.value = 0
    return graph ? [graph.cleanup] : []
  }

  function enqueueRemoteStreamCleanup(connection: ConnectionOwner): CleanupAction[] {
    connection.remotePlaybackId++
    const actions = enqueueAmplitudeCleanup(connection)

    const attachment = connection.remoteAttachment
    if (attachment) actions.push(attachment.cleanup)

    const stream = connection.remoteStream
    if (!stream) return actions

    actions.push(addCleanupAction(
      connection,
      "remote media stream",
      createMediaStreamRelease(stream, "remote media stream tracks"),
    ))
    connection.remoteStream = null
    return actions
  }

  function enqueueRemoteAudioCleanup(connection: ConnectionOwner) {
    const audio = connection.remoteAudio
    if (!audio) return

    addCleanupAction(connection, "remote audio pause", () => audio.pause())
    addCleanupAction(connection, "remote audio source", () => audio.removeAttribute("src"))
    addCleanupAction(connection, "remote audio reload", () => audio.load())
    addCleanupAction(connection, "remote audio element", () => audio.remove())
    connection.remoteAudio = null
  }

  function enqueueOwnedResourceCleanup(connection: ConnectionOwner) {
    enqueuePointerdownCleanup(connection)
    enqueueRemoteStreamCleanup(connection)
    enqueueRemoteAudioCleanup(connection)
    connection.resumeAttempt = null
  }

  function markClosing(
    connection: ConnectionOwner,
    updateState: boolean,
    attempt: ResourceReleaseAttempt,
  ) {
    if (!connection.closing) connection.closing = true

    abortConnection(connection, attempt)
    const transactionCleanup = connection.transactionCleanup
    if (transactionCleanup) startCleanupAction(connection, transactionCleanup, attempt)

    enqueueOwnedResourceCleanup(connection)
    for (const action of connection.cleanupOwner.actions) {
      startCleanupAction(connection, action, attempt)
    }
    if (updateState && !destroyed) state.value = "disconnected"
  }

  function trackOperation(connection: ConnectionOwner, physical: Promise<void>) {
    const owned: OwnedOperation = {
      promise: Promise.resolve(),
      failed: false,
      failure: undefined,
    }
    owned.promise = physical.then(
      () => {},
      (error) => {
        owned.failed = true
        owned.failure = error
      },
    ).finally(() => {
      if (!owned.failed) connection.operations.delete(owned)
    })
    connection.operations.add(owned)
  }

  function retainOperationFailure(
    connection: ConnectionOwner,
    failure: unknown,
  ): void {
    connection.operations.add({
      promise: Promise.resolve(),
      failed: true,
      failure,
    })
  }

  function startTeardown(
    connection: ConnectionOwner,
    updateState: boolean,
    attempt: ResourceReleaseAttempt = {},
  ): Promise<void> {
    if (connection.teardownOperation) {
      if (updateState && !destroyed) state.value = "disconnected"
      return connection.teardownOperation
    }
    if (connection.teardownAttempt === attempt) {
      return Promise.resolve()
    }

    connection.teardownAttempt = attempt
    connection.teardownFailed = false
    connection.teardownFailure = undefined
    markClosing(connection, updateState, attempt)

    const attemptedActions = new Set<CleanupAction>()
    const observedOperations = new Set<OwnedOperation>()
    let connectObserved = false
    const physicalOperation = Promise.resolve().then(async () => {
      const failures: unknown[] = []

      for (;;) {
        enqueueOwnedResourceCleanup(connection)
        try {
          await settleCleanupActions(
            connection,
            attempt,
            attemptedActions,
            connection.cleanupOwner.actions.filter(action => action !== connection.transactionCleanup),
          )
        } catch (error) {
          failures.push(error)
        }

        const connectOperation = connection.connectOperation
        if (connectOperation && !connectObserved) {
          connectObserved = true
          const controller = connection.controller
          if (
            !connection.connectSettled
            && controller !== null
            && !controller.signal.aborted
          ) {
            failures.push(new Error("WebRTC connection cancellation debt remains unresolved"))
          } else {
            const result = await Promise.allSettled([connectOperation])
            if (result[0]?.status === "rejected") failures.push(result[0].reason)
            continue
          }
        }

        const pendingOperations = [...connection.operations]
          .filter(operation => !observedOperations.has(operation))
        for (const operation of pendingOperations) observedOperations.add(operation)
        if (pendingOperations.length > 0) {
          await Promise.all(pendingOperations.map(operation => operation.promise))
          for (const operation of pendingOperations) {
            if (operation.failed) failures.push(operation.failure)
            connection.operations.delete(operation)
          }
          continue
        }

        enqueueOwnedResourceCleanup(connection)
        if (connection.cleanupOwner.actions.some(
          action => action !== connection.transactionCleanup && !attemptedActions.has(action),
        )) continue

        const transactionCleanup = connection.transactionCleanup
        if (transactionCleanup && !attemptedActions.has(transactionCleanup)) {
          try {
            await settleCleanupActions(connection, attempt, attemptedActions, [transactionCleanup])
          } catch (error) {
            failures.push(error)
          }
          continue
        }
        break
      }

      const unresolvedCleanup = !connection.cleanupOwner.settled
      if (!unresolvedCleanup && owner === connection) owner = null
      if (failures.length > 0) throw new AggregateError(failures, "WebRTC teardown failed")
      if (unresolvedCleanup) {
        throw new Error("WebRTC teardown left unresolved cleanup actions")
      }
    })

    let operation!: Promise<void>
    operation = physicalOperation.then(undefined, (cleanupError) => {
      let failure = cleanupError
      try {
        reportCleanupError(cleanupError)
      } catch (reportingError) {
        failure = new AggregateError(
          [cleanupError, reportingError],
          "WebRTC cleanup and cleanup reporting failed",
        )
      }
      connection.teardownFailed = true
      connection.teardownFailure = failure
    }).finally(() => {
      if (connection.teardownOperation === operation) connection.teardownOperation = null
    })
    connection.teardownOperation = operation
    return operation
  }

  async function awaitTeardown(
    connection: ConnectionOwner,
    updateState: boolean,
    attempt: ResourceReleaseAttempt = {},
  ) {
    await startTeardown(connection, updateState, attempt)
    if (connection.teardownFailed) throw connection.teardownFailure
  }

  function terminalFailure(
    connection: ConnectionOwner,
    type: string,
    error: unknown,
    attempt: ResourceReleaseAttempt = {},
  ) {
    if (!isCurrent(connection)) return

    try {
      reportClientEvent(type, errorMessage(error))
    } catch (reportingFailure) {
      retainOperationFailure(connection, reportingFailure)
    }
    if (!destroyed) state.value = "error"
    startTeardown(connection, false, attempt)
  }

  function stopPointerdownRetry(
    connection: ConnectionOwner,
    attempt: ResourceReleaseAttempt,
  ) {
    const action = enqueuePointerdownCleanup(connection)
    if (!action) return
    const cleanup = settleCleanupActions(connection, attempt, new Set(), [action])
      .catch(error => terminalFailure(
        connection,
        "webrtc_audio_resume_error",
        error,
        attempt,
      ))
    trackOperation(connection, cleanup)
  }

  function armAudioResumeRetry(
    connection: ConnectionOwner,
    context: AudioContext,
    setupAttempt: ResourceReleaseAttempt,
  ) {
    let contextRunning: boolean
    try {
      contextRunning = context.state === "running"
    } catch (error) {
      terminalFailure(connection, "webrtc_audio_resume_error", error, setupAttempt)
      return
    }
    if (
      !isCurrent(connection)
      || connection.context !== context
      || contextRunning
      || connection.pointerdownRetryHandler
    ) return

    const retry = () => {
      const callbackAttempt: ResourceReleaseAttempt = {}
      try {
        stopPointerdownRetry(connection, callbackAttempt)
        startAudioResume(connection, context, true, callbackAttempt)
      } catch (error) {
        terminalFailure(connection, "webrtc_audio_resume_error", error, callbackAttempt)
      }
    }
    connection.pointerdownRetryHandler = retry

    const listenerResources = acquireResourceGroup(
      connection,
      "audio resume listener",
      "WebRTC audio resume listener",
      action => { connection.pointerdownRetryCleanup = action },
      action => {
        if (connection.pointerdownRetryCleanup === action) {
          connection.pointerdownRetryCleanup = null
          connection.pointerdownRetryHandler = null
        }
      },
    )
    try {
      listenerResources.acquire(
        () => document.addEventListener("pointerdown", retry, { once: true }),
        () => document.removeEventListener("pointerdown", retry),
        "audio resume listener registration",
        setupAttempt,
      )
    } catch (error) {
      terminalFailure(connection, "webrtc_audio_resume_error", error, setupAttempt)
    }
  }

  function startAudioResume(
    connection: ConnectionOwner,
    context: AudioContext,
    fromUserGesture = false,
    releaseAttempt: ResourceReleaseAttempt = {},
  ) {
    let contextRunning: boolean
    try {
      contextRunning = context.state === "running"
    } catch (error) {
      terminalFailure(connection, "webrtc_audio_resume_error", error, releaseAttempt)
      return
    }
    if (!isCurrent(connection) || connection.context !== context || contextRunning) return

    const currentResume = connection.resumeAttempt
    if (currentResume?.context === context) {
      if (fromUserGesture) currentResume.retryAttempt = releaseAttempt
      return
    }

    const resume: AudioResumeAttempt = { context, retryAttempt: null }
    connection.resumeAttempt = resume
    const physical = Promise.resolve().then(async () => {
      let attempt = releaseAttempt
      for (;;) {
        resume.retryAttempt = null
        try {
          await context.resume()
        } catch (error) {
          if (isCurrent(connection) && connection.context === context) {
            try {
              reportClientEvent("webrtc_audio_resume_error", errorMessage(error))
            } catch (reportingFailure) {
              throw new AggregateError(
                [error, reportingFailure],
                "Audio resume and failure reporting failed",
              )
            }
          }
        }

        if (!isCurrent(connection) || connection.context !== context) return
        if (context.state === "running") {
          stopPointerdownRetry(connection, attempt)
          return
        }
        const retryAttempt = resume.retryAttempt
        if (retryAttempt) {
          attempt = retryAttempt
          continue
        }
        armAudioResumeRetry(connection, context, attempt)
        return
      }
    })

    const observed = physical.catch((error) => {
      terminalFailure(connection, "webrtc_audio_resume_error", error, releaseAttempt)
    })
    trackOperation(connection, observed.finally(() => {
      if (connection.resumeAttempt === resume) connection.resumeAttempt = null
    }))
  }

  function getOrCreateRemoteAudio(
    connection: ConnectionOwner,
    transactionResources: ResourceOwner,
    setupAttempt: ResourceReleaseAttempt,
  ): HTMLAudioElement {
    if (connection.remoteAudio) return connection.remoteAudio

    let partialAudio: HTMLAudioElement | undefined
    const acquisition = transactionResources.acquire(
      () => {
        const audio = document.createElement("audio")
        partialAudio = audio
        audio.autoplay = true
        audio.setAttribute("playsinline", "")
        audio.style.display = "none"
        document.body.appendChild(audio)
        return audio
      },
      (audio) => (audio ?? partialAudio)?.remove(),
      "remote audio element construction",
      setupAttempt,
    )
    const audio = acquisition.value
    connection.remoteAudio = audio
    acquisition.transfer()
    return audio
  }

  async function attachRemoteStream(
    connection: ConnectionOwner,
    audio: HTMLAudioElement,
    stream: MediaStream,
    attempt: ResourceReleaseAttempt,
  ): Promise<void> {
    const current = connection.remoteAttachment
    if (current?.audio === audio && current.stream === stream) return
    if (current) {
      await settleCleanupActions(connection, attempt, new Set(), [current.cleanup])
    }
    if (!isCurrent(connection) || connection.remoteAudio !== audio) return

    let acquisitionPending = true
    let settleAcquisition!: () => void
    const acquisitionSettled = new Promise<void>((resolve) => {
      settleAcquisition = resolve
    })
    let attachment!: RemoteAudioAttachment
    const cleanup = addCleanupAction(
      connection,
      "remote audio stream attachment",
      async () => {
        if (acquisitionPending) await acquisitionSettled
        if (audio.srcObject === stream) audio.srcObject = null
        if (connection.remoteAttachment === attachment) connection.remoteAttachment = null
      },
    )
    attachment = { audio, stream, cleanup }
    connection.remoteAttachment = attachment

    let setupOutcome: { failed: false } | { failed: true; failure: unknown } = {
      failed: false,
    }
    try {
      audio.srcObject = stream
    } catch (error) {
      setupOutcome = { failed: true, failure: error }
    } finally {
      acquisitionPending = false
      settleAcquisition()
    }

    if (!setupOutcome.failed) return
    const setupFailure = setupOutcome.failure
    if (connection.closing) throw setupFailure

    try {
      await settleCleanupActions(connection, attempt, new Set(), [cleanup])
    } catch (cleanupFailure) {
      throw new AggregateError(
        [setupFailure, cleanupFailure],
        "Remote audio attachment and rollback failed",
      )
    }
    throw setupFailure
  }

  function acquireRemoteStream(
    transactionResources: ResourceOwner,
    event: RTCTrackEvent,
    attempt: ResourceReleaseAttempt,
  ): RemoteStreamCandidate {
    const stream = event.streams?.[0]
    if (stream) return { stream, fallbackClaim: null }

    const acquisition = transactionResources.acquire(
      () => new MediaStream([event.track]),
      acquiredStream => {
        if (acquiredStream) {
          createMediaStreamRelease(
            acquiredStream,
            "fallback remote media stream tracks",
          )()
        } else {
          event.track.stop()
        }
      },
      "fallback remote media stream construction",
      attempt,
    )
    return {
      stream: acquisition.value,
      fallbackClaim: acquisition,
    }
  }

  async function setupRemoteAmplitude(
    connection: ConnectionOwner,
    transactionResources: ResourceOwner,
    stream: MediaStream,
    context: AudioContext,
    playbackId: number,
    setupAttempt: ResourceReleaseAttempt,
  ) {
    const previousActions = enqueueAmplitudeCleanup(connection)
    const previousPointerdown = enqueuePointerdownCleanup(connection)
    if (previousPointerdown) previousActions.push(previousPointerdown)
    await settleCleanupActions(connection, setupAttempt, new Set(), previousActions)
    if (
      !isCurrent(connection)
      || connection.transactionResources !== transactionResources
      || connection.context !== context
      || connection.remoteStream !== stream
      || connection.remotePlaybackId !== playbackId
    ) return

    let setupPending = true
    let settleSetup!: () => void
    const setupSettled = new Promise<void>((resolve) => {
      settleSetup = resolve
    })

    let graph!: RemoteAmplitudeGraph
    const cleanup = addCleanupAction(
      connection,
      "remote amplitude graph",
      async (attempt) => {
        if (setupPending) await setupSettled
        const resources = graph.resources
        if (resources) {
          resources.dispose(attempt)
          if (!resources.settled) {
            throw new Error("Remote amplitude graph cleanup remains unsettled")
          }
        }
        if (connection.remoteAmplitudeGraph === graph) {
          connection.remoteAmplitudeGraph = null
        }
        if (!destroyed) mouthAmplitude.value = 0
      },
    )
    graph = {
      stream,
      context,
      resources: null,
      source: null,
      analyser: null,
      cleanup,
    }
    // The graph claim is authoritative before ResourceOwner/AudioNode host calls.
    // Teardown waits for this exact setup to leave its re-entrant critical section,
    // then disposes the complete graph once in reverse acquisition order.
    connection.remoteAmplitudeGraph = graph

    let setupOutcome: { failed: false } | { failed: true; failure: unknown } = {
      failed: false,
    }
    try {
      const resources = createResourceOwner("WebRTC remote amplitude graph")
      graph.resources = resources
      if (
        !isCurrent(connection)
        || connection.transactionResources !== transactionResources
        || connection.remoteAmplitudeGraph !== graph
      ) return

      const sourceAcquisition = resources.acquire(
        () => context.createMediaStreamSource(stream),
        source => source?.disconnect(),
        "remote audio source construction",
        setupAttempt,
      )
      const source = sourceAcquisition.value
      graph.source = source
      if (
        !isCurrent(connection)
        || connection.transactionResources !== transactionResources
        || connection.remoteAmplitudeGraph !== graph
      ) return

      const analyserAcquisition = resources.acquire(
        () => context.createAnalyser(),
        analyser => analyser?.disconnect(),
        "remote audio analyser construction",
        setupAttempt,
      )
      const analyser = analyserAcquisition.value
      graph.analyser = analyser
      if (
        !isCurrent(connection)
        || connection.transactionResources !== transactionResources
        || connection.remoteAmplitudeGraph !== graph
      ) return

      analyser.fftSize = 512
      if (!isCurrent(connection) || connection.remoteAmplitudeGraph !== graph) return
      analyser.smoothingTimeConstant = 0.2
      if (!isCurrent(connection) || connection.remoteAmplitudeGraph !== graph) return
      analyser.minDecibels = -70
      if (!isCurrent(connection) || connection.remoteAmplitudeGraph !== graph) return
      analyser.maxDecibels = -20
      if (!isCurrent(connection) || connection.remoteAmplitudeGraph !== graph) return
      source.connect(analyser)
      if (!isCurrent(connection) || connection.remoteAmplitudeGraph !== graph) return

      startAudioResume(connection, context, false, setupAttempt)
      armAudioResumeRetry(connection, context, setupAttempt)

      const binHz = context.sampleRate / analyser.fftSize
      const lowBin = Math.max(0, Math.round(300 / binHz))
      const highBin = Math.min(analyser.frequencyBinCount - 1, Math.round(3400 / binHz))
      const binCount = highBin - lowBin + 1
      const buffer = new Uint8Array(analyser.frequencyBinCount)
      const NOISE_FLOOR = 0.22

      const scheduleTick = (acquisitionAttempt: ResourceReleaseAttempt) => {
        createOwnedAnimationFrame(
          resources,
          "remote amplitude frame",
          (_timestamp, callbackAttempt) => {
            if (
              !isCurrent(connection)
              || connection.remoteAmplitudeGraph !== graph
              || graph.analyser !== analyser
              || connection.context !== context
            ) return

            try {
              analyser.getByteFrequencyData(buffer)
              if (
                !isCurrent(connection)
                || connection.remoteAmplitudeGraph !== graph
                || graph.analyser !== analyser
                || connection.context !== context
              ) return

              let sum = 0
              for (let index = lowBin; index <= highBin; index++) sum += buffer[index]
              const average = sum / (binCount * 255)
              mouthAmplitude.value = average < NOISE_FLOOR
                ? 0
                : Math.min(1, (average - NOISE_FLOOR) * 3.5)
              scheduleTick(callbackAttempt)
            } catch (error) {
              terminalFailure(
                connection,
                "webrtc_remote_audio_error",
                error,
                callbackAttempt,
              )
            }
          },
          acquisitionAttempt,
        )
      }

      scheduleTick(setupAttempt)
    } catch (error) {
      setupOutcome = { failed: true, failure: error }
    } finally {
      setupPending = false
      settleSetup()
    }

    if (!setupOutcome.failed) return
    const primaryFailure = setupOutcome.failure
    if (connection.closing) throw primaryFailure

    const rollbackActions: CleanupAction[] = [cleanup]
    const pointerdown = enqueuePointerdownCleanup(connection)
    if (pointerdown) rollbackActions.push(pointerdown)
    try {
      await settleCleanupActions(
        connection,
        setupAttempt,
        new Set(),
        rollbackActions,
      )
    } catch (cleanupError) {
      throw new AggregateError(
        [primaryFailure, cleanupError],
        "Remote amplitude setup and rollback failed",
      )
    }
    throw primaryFailure
  }

  async function cleanupUntransferredTrack(
    connection: ConnectionOwner,
    track: MediaStreamTrack,
    attempt: ResourceReleaseAttempt,
  ) {
    const action = addCleanupAction(connection, "stale remote media track", () => track.stop())
    await settleCleanupActions(connection, attempt, new Set(), [action])
  }

  async function cleanupRemoteStreamCandidate(
    connection: ConnectionOwner,
    candidate: RemoteStreamCandidate,
    eventTrack: MediaStreamTrack,
    attempt: ResourceReleaseAttempt,
  ) {
    if (candidate.fallbackClaim) {
      candidate.fallbackClaim.release(attempt)
      return
    }
    await cleanupUntransferredTrack(connection, eventTrack, attempt)
  }

  async function handleRemoteTrack(
    connection: ConnectionOwner,
    transactionResources: ResourceOwner,
    transport: PeerTransport,
    context: AudioContext,
    event: RTCTrackEvent,
    callbackAttempt: ResourceReleaseAttempt,
    sendReady: (attempt: ResourceReleaseAttempt) => boolean,
  ) {
    if (
      !isCurrent(connection)
      || connection.transactionResources !== transactionResources
      || connection.transport !== transport
      || connection.context !== context
    ) {
      await cleanupUntransferredTrack(connection, event.track, callbackAttempt)
      return
    }

    const candidate = acquireRemoteStream(transactionResources, event, callbackAttempt)
    const remoteStream = candidate.stream

    if (connection.remoteStream !== remoteStream) {
      const previousActions = enqueueRemoteStreamCleanup(connection)
      if (
        !isCurrent(connection)
        || connection.transactionResources !== transactionResources
        || connection.transport !== transport
        || connection.context !== context
      ) {
        await cleanupRemoteStreamCandidate(
          connection,
          candidate,
          event.track,
          callbackAttempt,
        )
        return
      }
      // Claim the incoming stream before the first await so a concurrent track event or
      // teardown always has one authoritative owner that can release it.
      connection.remoteStream = remoteStream
      candidate.fallbackClaim?.transfer()
      await settleCleanupActions(
        connection,
        callbackAttempt,
        new Set(),
        previousActions,
      )
      if (
        !isCurrent(connection)
        || connection.transactionResources !== transactionResources
        || connection.transport !== transport
        || connection.context !== context
        || connection.remoteStream !== remoteStream
      ) return
    } else {
      candidate.fallbackClaim?.transfer()
    }
    const playbackId = ++connection.remotePlaybackId

    const audio = getOrCreateRemoteAudio(connection, transactionResources, callbackAttempt)
    await attachRemoteStream(connection, audio, remoteStream, callbackAttempt)
    if (
      !isCurrent(connection)
      || connection.transactionResources !== transactionResources
      || connection.remoteAudio !== audio
      || connection.remoteAttachment?.audio !== audio
      || connection.remoteAttachment.stream !== remoteStream
    ) return
    await audio.play()

    if (
      !isCurrent(connection)
      || connection.transactionResources !== transactionResources
      || connection.transport !== transport
      || connection.context !== context
      || connection.remoteAudio !== audio
      || connection.remoteAttachment?.audio !== audio
      || connection.remoteAttachment.stream !== remoteStream
      || connection.remoteStream !== remoteStream
      || connection.remotePlaybackId !== playbackId
    ) return

    const channel = transport.channel
    if (!channel) return
    if (channel.readyState === "open") {
      if (!sendReady(callbackAttempt)) return
    } else if (channel.readyState === "connecting") {
      transport.readyPending = true
    } else {
      terminalFailure(
        connection,
        "webrtc_data_channel_unavailable",
        new Error(`Data channel state: ${channel.readyState}`),
        callbackAttempt,
      )
      return
    }

    await setupRemoteAmplitude(
      connection,
      transactionResources,
      remoteStream,
      context,
      playbackId,
      callbackAttempt,
    )
  }

  function handleDataChannelMessage(connection: ConnectionOwner, event: MessageEvent) {
    if (!isCurrent(connection)) return
    let message: DataChannelMessage
    try {
      const parsed = JSON.parse(event.data as string) as unknown
      if (!parsed || typeof parsed !== "object" || typeof (parsed as { type?: unknown }).type !== "string") {
        throw new Error("message must be an object with a string type")
      }
      message = parsed as DataChannelMessage
    } catch (error) {
      reportClientEvent("webrtc_message_parse_error", errorMessage(error))
      return
    }

    try {
      switch (message.type) {
        case "transcript": callbacks.onTranscript(message.text ?? ""); break
        case "subtitle": callbacks.onSubtitle?.(message.text ?? "", message.durationMs ?? 0); break
        case "agent_reply": callbacks.onReply(message.text ?? ""); break
        case "agent_cancelled": callbacks.onCancelled?.(); break
        case "bot_speaking": callbacks.onBotSpeaking?.(); break
        case "bot_silent": callbacks.onBotSilent?.(); break
        case "user_speaking": callbacks.onUserSpeaking?.(); break
        case "user_silent": callbacks.onUserSilent?.(); break
        case "end_conversation": callbacks.onEndConversation?.(); break
      }
    } catch (error) {
      reportClientEvent("webrtc_callback_error", `${message.type}: ${errorMessage(error)}`)
    }
  }

  async function runConnect(
    connection: ConnectionOwner,
    sessionId: string,
    setup: ConnectionSetup,
  ) {
    let phase: "microphone" | "ice" | "connection" = "microphone"

    try {
      if (!isOwnedOpen(connection)) return
      const controller = acquireConnectionController(connection)
      if (!isCurrent(connection) || connection.controller !== controller) return

      const transactionResources = acquireTransactionResources(connection)
      if (!isCurrent(connection) || connection.transactionResources !== transactionResources) return

      const stream = await acquireLocalStream(connection)
      if (!isCurrent(connection)) return

      phase = "connection"
      const context = await acquireAudioContext(connection)
      if (!isCurrent(connection)) return

      phase = "ice"
      const iceResponse = await fetch(`${apiBaseUrl}/api/voice/ice-servers`, {
        signal: controller.signal,
      })
      if (!iceResponse.ok) throw new Error(`GET /api/voice/ice-servers -> ${iceResponse.status}`)
      const payload = await readJsonResponse<IceServersResponse>(
        iceResponse,
        controller.signal,
      )
      if (!Array.isArray(payload.iceServers)) throw new Error("Invalid ICE server response")
      if (!isCurrent(connection)) return

      phase = "connection"
      const peerSetup = beginPeerTransportSetup(connection)
      const { transport, resources: transportResources } = peerSetup
      let peer!: RTCPeerConnection
      let channel!: RTCDataChannel
      let sendReady!: (attempt: ResourceReleaseAttempt) => boolean

      try {
        if (!isCurrentTransport(connection, transport)) return
        const peerAcquisition = transportResources.acquire(
          () => new RTCPeerConnection({ iceServers: payload.iceServers }),
          acquiredPeer => acquiredPeer?.close(),
          "peer connection construction",
          setup.releaseAttempt,
        )
        peer = peerAcquisition.value
        transport.peer = peer
        if (!isCurrentTransport(connection, transport)) return

        for (const track of stream.getTracks()) {
          const sender = peer.addTrack(track, stream)
          if (!isCurrentTransport(connection, transport)) return
          const transceivers = peer.getTransceivers()
          if (!isCurrentTransport(connection, transport)) return
          const transceiver = transceivers.find(candidate => candidate.sender === sender)
          if (transceiver) {
            transceiver.direction = "sendrecv"
            if (!isCurrentTransport(connection, transport)) return
          }
        }

        const channelAcquisition = transportResources.acquire(
          () => peer.createDataChannel("app-messages", { ordered: true }),
          acquiredChannel => acquiredChannel?.close(),
          "data channel construction",
          setup.releaseAttempt,
        )
        channel = channelAcquisition.value
        transport.channel = channel
        if (!isCurrentTransport(connection, transport)) return

        sendReady = (attempt: ResourceReleaseAttempt): boolean => {
          try {
            if (
              !isCurrentTransport(connection, transport)
              || transport.channel !== channel
              || channel.readyState !== "open"
            ) return false
            channel.send(JSON.stringify({ type: "client_ready" }))
            return true
          } catch (error) {
            terminalFailure(connection, "webrtc_data_channel_send_error", error, attempt)
            return false
          }
        }

        const onOpen = () => {
          if (!isCurrentTransport(connection, transport) || !transport.readyPending) return
          transport.readyPending = false
          sendReady({})
        }
        transportResources.acquire(
          () => {
            channel.onopen = onOpen
            return onOpen
          },
          () => { channel.onopen = null },
          "data channel open callback",
          setup.releaseAttempt,
        )
        if (!isCurrentTransport(connection, transport)) return

        const onMessage = (event: MessageEvent) => handleDataChannelMessage(connection, event)
        transportResources.acquire(
          () => {
            channel.onmessage = onMessage
            return onMessage
          },
          () => { channel.onmessage = null },
          "data channel message callback",
          setup.releaseAttempt,
        )
        if (!isCurrentTransport(connection, transport)) return

        const onTrack = (event: RTCTrackEvent) => {
          const callbackAttempt: ResourceReleaseAttempt = {}
          const physical = Promise.resolve().then(() => handleRemoteTrack(
            connection,
            transactionResources,
            transport,
            context,
            event,
            callbackAttempt,
            sendReady,
          )).catch((error) => {
            if (isCurrentTransport(connection, transport)) {
              terminalFailure(
                connection,
                "webrtc_remote_audio_error",
                error,
                callbackAttempt,
              )
              return
            }
            if (!(error instanceof ResourceOwnerClosedError)) throw error
          })
          trackOperation(connection, physical)
        }
        transportResources.acquire(
          () => {
            peer.ontrack = onTrack
            return onTrack
          },
          () => { peer.ontrack = null },
          "peer track callback",
          setup.releaseAttempt,
        )
        if (!isCurrentTransport(connection, transport)) return

        const onIceConnectionStateChange = () => {
          if (!isCurrentTransport(connection, transport) || transport.peer !== peer) return
          const callbackAttempt: ResourceReleaseAttempt = {}
          try {
            const next = peer.iceConnectionState
            if (next === "connected" || next === "completed") {
              state.value = "connected"
            } else if (next === "failed" || next === "closed") {
              if (next === "failed") {
                reportClientEvent("webrtc_ice_failed", `ICE connection state: ${next}`)
              }
              if (!destroyed) state.value = "error"
              startTeardown(connection, false, callbackAttempt)
            }
          } catch (error) {
            terminalFailure(connection, "webrtc_ice_state_error", error, callbackAttempt)
          }
        }
        transportResources.acquire(
          () => {
            peer.oniceconnectionstatechange = onIceConnectionStateChange
            return onIceConnectionStateChange
          },
          () => { peer.oniceconnectionstatechange = null },
          "peer ICE state callback",
          setup.releaseAttempt,
        )
      } finally {
        peerSetup.finish()
      }

      if (!isCurrentTransport(connection, transport)) return
      const offer = await peer.createOffer()
      if (!isCurrentTransport(connection, transport) || transport.peer !== peer) return
      await peer.setLocalDescription(offer)
      await waitForIceGathering(connection, peer, setup, () => {
        if (isCurrentTransport(connection, transport) && transport.peer === peer) {
          reportClientEvent(
            "webrtc_ice_gathering_timeout",
            `ICE gathering state: ${peer.iceGatheringState}`,
          )
        }
      })
      if (!isCurrentTransport(connection, transport) || transport.peer !== peer) return

      const response = await fetch(`${apiBaseUrl}/api/voice/offer`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          sdp: peer.localDescription!.sdp,
          type: peer.localDescription!.type,
          pc_id: transport.pcId,
          session_id: sessionId,
        }),
        signal: controller.signal,
      })
      if (!isCurrentTransport(connection, transport) || transport.peer !== peer) return
      if (!response.ok) throw new Error(`POST /api/voice/offer -> ${response.status}`)

      const answer = await readJsonResponse<{
        sdp: string
        type: RTCSdpType
        pc_id: string
      }>(response, controller.signal)
      if (!isCurrentTransport(connection, transport) || transport.peer !== peer) return
      transport.pcId = answer.pc_id
      await peer.setRemoteDescription({ sdp: answer.sdp, type: answer.type })
    } catch (error) {
      if (!isCurrent(connection)) {
        if (connection.closing && !(error instanceof ResourceAcquisitionRollbackError)) return
        throw error
      }

      const name = error instanceof DOMException ? error.name : ""
      let reportingOutcome: { failed: false } | { failed: true; failure: unknown } = {
        failed: false,
      }
      try {
        if (phase === "microphone" && (name === "NotAllowedError" || name === "PermissionDeniedError")) {
          micDenied.value = true
          reportClientEvent("webrtc_mic_denied", "getUserMedia permission denied", name)
        } else if (phase === "microphone") {
          reportClientEvent("webrtc_mic_error", errorMessage(error), name)
        } else if (phase === "ice") {
          reportClientEvent("webrtc_ice_config_error", errorMessage(error))
        } else {
          reportClientEvent("webrtc_connect_error", errorMessage(error))
        }
      } catch (failure) {
        reportingOutcome = { failed: true, failure }
      }

      connection.connectTerminalFailed = true
      connection.connectTerminalFailure = error
      if (!destroyed) state.value = "error"
      markClosing(connection, false, setup.releaseAttempt)
      if (reportingOutcome.failed) {
        throw new AggregateError(
          [error, reportingOutcome.failure],
          "WebRTC connection failure reporting failed",
        )
      }
    }
  }

  async function connect(sessionId: string): Promise<void> {
    if (destroyed || state.value === "connecting" || state.value === "connected") return
    const requestId = ++lifecycleId

    const existing = owner
    if (existing) {
      await awaitTeardown(existing, false)
      if (requestId !== lifecycleId || destroyed) return
    }

    const connection: ConnectionOwner = {
      controller: null,
      cancellationCleanup: null,
      transactionResources: null,
      transactionCleanup: null,
      stream: null,
      localStreamCleanup: null,
      context: null,
      audioContextCleanup: null,
      transport: null,
      remoteStream: null,
      remoteAudio: null,
      remoteAttachment: null,
      remoteAmplitudeGraph: null,
      remotePlaybackId: 0,
      pointerdownRetryHandler: null,
      pointerdownRetryCleanup: null,
      resumeAttempt: null,
      connectOperation: null,
      connectSettled: false,
      connectTerminalFailed: false,
      connectTerminalFailure: undefined,
      operations: new Set(),
      cleanupOwner: createAsyncReleaseOwner("WebRTC resources"),
      closing: false,
      teardownOperation: null,
      teardownAttempt: null,
      teardownFailed: false,
      teardownFailure: undefined,
    }
    owner = connection

    const setup: ConnectionSetup = { releaseAttempt: {} }
    let operation!: Promise<void>
    operation = Promise.resolve()
      .then(() => runConnect(connection, sessionId, setup))
      .then(
        () => { connection.connectSettled = true },
        (error) => {
          connection.connectSettled = true
          throw error
        },
      )
      .finally(() => {
        if (connection.connectOperation === operation) connection.connectOperation = null
      })
    connection.connectOperation = operation

    state.value = "connecting"
    micDenied.value = false

    let connectionOutcome: { failed: false } | { failed: true; failure: unknown } = {
      failed: false,
    }
    try {
      await operation
    } catch (error) {
      connectionOutcome = { failed: true, failure: error }
    }

    let teardownOutcome: { failed: false } | { failed: true; failure: unknown } = {
      failed: false,
    }
    if (connection.closing) {
      try {
        await awaitTeardown(connection, false, setup.releaseAttempt)
      } catch (error) {
        teardownOutcome = { failed: true, failure: error }
      }
    }

    if (connectionOutcome.failed && teardownOutcome.failed) {
      throw new AggregateError(
        [connectionOutcome.failure, teardownOutcome.failure],
        "WebRTC teardown failed after connection failure",
      )
    }
    if (
      !connectionOutcome.failed
      && connection.connectTerminalFailed
      && teardownOutcome.failed
    ) {
      throw new AggregateError(
        [connection.connectTerminalFailure, teardownOutcome.failure],
        "WebRTC operation and teardown failed",
      )
    }
    if (connectionOutcome.failed) throw connectionOutcome.failure
    if (teardownOutcome.failed) throw teardownOutcome.failure
  }

  async function disconnect(): Promise<void> {
    lifecycleId++
    const connection = owner
    if (!connection) {
      if (!destroyed) state.value = "disconnected"
      return
    }
    await awaitTeardown(connection, true)
  }

  observeAsynchronousScopeTeardown(
    "WebRTC asynchronous teardown",
    async (attempt) => {
      destroyed = true
      lifecycleId++
      const connection = owner
      if (connection) await awaitTeardown(connection, false, attempt)
    },
    reportObservedTeardownFailure,
  )

  return {
    state: readonly(state),
    mouthAmplitude: readonly(mouthAmplitude),
    micDenied: readonly(micDenied),
    connect,
    disconnect,
  }
}
