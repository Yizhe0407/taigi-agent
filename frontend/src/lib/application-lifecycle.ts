import type { App } from "vue"

import {
  consumeRetainedSynchronousTeardownFailure,
  runWithSynchronousScopeReleaseAttempt,
  settleRetainedAsynchronousTeardowns,
  settleRetainedSynchronousTeardowns,
} from "@/lib/component-lifecycle"
import {
  createResourceOwner,
  type ResourceReleaseAttempt,
} from "@/lib/resource-owner"

export type ApplicationLifecycle = Readonly<{
  closing: boolean
  started: boolean
  start(): Promise<void>
  teardown(): Promise<void>
  requestObservedTeardown(info: string): void
}>

type ApplicationLifecycleOptions = {
  app: App
  mountTarget: string | Element
  reportClientEvent(type: string, message: string, detail?: string): void
  shutdownClientEventReporting(attempt: ResourceReleaseAttempt): Promise<void>
  windowTarget?: Window
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

function throwFailures(failures: unknown[], message: string): void {
  if (failures.length === 1) throw failures[0]
  if (failures.length > 1) throw new AggregateError(failures, message)
}

/**
 * Owns all document-lifespan application resources behind one permanent close
 * gate. Setup publishes exact rollback claims before host calls; teardown joins
 * one physical operation and retains only failed claims for a later retry.
 */
export function createApplicationLifecycle({
  app,
  mountTarget,
  reportClientEvent,
  shutdownClientEventReporting,
  windowTarget = window,
}: ApplicationLifecycleOptions): ApplicationLifecycle {
  const resources = createResourceOwner("application document lifecycle")

  let started = false
  let teardownComplete = false
  let startOperation: Promise<void> | null = null
  let teardownOperation: Promise<void> | null = null
  let observedTeardown: Promise<void> | null = null
  let publicTeardownOperation: Promise<void> | null = null
  const retainedObservedTeardownFailures: unknown[] = []
  let clientEventReportingShutdown = false

  function handleWindowError(event: ErrorEvent): void {
    if (resources.disposed) return
    reportClientEvent(
      "window_error",
      event.message || String(event.error),
      `${event.filename}:${event.lineno}:${event.colno}\n${event.error?.stack ?? ""}`,
    )
  }

  function handleUnhandledRejection(event: PromiseRejectionEvent): void {
    if (resources.disposed) return
    const reason = event.reason
    reportClientEvent(
      "unhandled_rejection",
      reason instanceof Error ? reason.message : String(reason),
      reason instanceof Error ? reason.stack : undefined,
    )
  }

  function reportUnhandledFailure(error: unknown): void {
    if (typeof windowTarget.reportError === "function") {
      try {
        windowTarget.reportError(error)
      } catch (reportingError) {
        throw new AggregateError(
          [error, reportingError],
          "Application failure and the global error reporter both failed",
        )
      }
      return
    }
    throw error
  }

  function reportObservedFailure(error: unknown, info: string): void {
    const handler = app.config.errorHandler
    if (!handler) {
      reportUnhandledFailure(error)
      return
    }
    try {
      handler(error, null, info)
    } catch (handlerError) {
      reportUnhandledFailure(
        new AggregateError(
          [error, handlerError],
          "Application teardown and its error handler both failed",
        ),
      )
    }
  }

  function requestObservedTeardown(info: string): void {
    if (observedTeardown) return

    let resolveObservation!: () => void
    const observer = new Promise<void>((resolve) => {
      resolveObservation = resolve
    })
    // Publish observation ownership before requestTeardown() can synchronously
    // re-enter through AbortController listeners.
    observedTeardown = observer

    const finishObservation = () => {
      if (observedTeardown === observer) observedTeardown = null
      resolveObservation()
    }
    let teardown: Promise<void>
    try {
      teardown = requestTeardown()
    } catch (error) {
      try {
        reportObservedFailure(error, info)
      } catch (reportingError) {
        retainedObservedTeardownFailures.push(new AggregateError(
          [error, reportingError],
          "Application observed teardown and failure reporting both failed",
        ))
      }
      finishObservation()
      return
    }

    void teardown.then(
      finishObservation,
      (error) => {
        try {
          reportObservedFailure(error, info)
        } catch (reportingError) {
          retainedObservedTeardownFailures.push(new AggregateError(
            [error, reportingError],
            "Application observed teardown and failure reporting both failed",
          ))
        }
        finishObservation()
      },
    )
  }

  function handlePageHide(event: PageTransitionEvent): void {
    if (event.persisted || resources.disposed) return
    requestObservedTeardown("application pagehide teardown")
  }

  function registerWindowResources(attempt: ResourceReleaseAttempt): void {
    resources.acquire(
      () => windowTarget.addEventListener("error", handleWindowError),
      () => windowTarget.removeEventListener("error", handleWindowError),
      "window error listener",
      attempt,
    )
    resources.acquire(
      () => windowTarget.addEventListener("unhandledrejection", handleUnhandledRejection),
      () => windowTarget.removeEventListener("unhandledrejection", handleUnhandledRejection),
      "window unhandled rejection listener",
      attempt,
    )
    resources.acquire(
      () => windowTarget.addEventListener("pagehide", handlePageHide),
      () => windowTarget.removeEventListener("pagehide", handlePageHide),
      "window pagehide listener",
      attempt,
    )
  }

  async function runTeardown(
    closeFailures: readonly unknown[],
    attempt: ResourceReleaseAttempt,
  ): Promise<void> {
    const failures: unknown[] = []

    for (const closeFailure of closeFailures) {
      failures.push(new Error(`browser close gate: ${errorMessage(closeFailure)}`, {
        cause: closeFailure,
      }))
    }

    // Retry debt from earlier component unmounts before this application
    // teardown begins unmounting the current tree. Debt created by this exact
    // unmount attempt is observed below and left for the next public teardown.
    try {
      settleRetainedSynchronousTeardowns(attempt)
    } catch {
      // consumeRetainedSynchronousTeardownFailure() below is the authoritative
      // delivery after browser resources have had their one release attempt.
    }

    try {
      resources.dispose(attempt)
    } catch (error) {
      failures.push(new Error(`browser resources: ${errorMessage(error)}`, { cause: error }))
    }

    const componentTeardownFailure = consumeRetainedSynchronousTeardownFailure()
    if (componentTeardownFailure !== null) {
      failures.push(new Error(
        `component resources: ${errorMessage(componentTeardownFailure)}`,
        { cause: componentTeardownFailure },
      ))
    }

    try {
      await settleRetainedAsynchronousTeardowns(attempt)
    } catch (error) {
      failures.push(new Error(
        `asynchronous component resources: ${errorMessage(error)}`,
        { cause: error },
      ))
    }

    if (!clientEventReportingShutdown) {
      try {
        await shutdownClientEventReporting(attempt)
        clientEventReportingShutdown = true
      } catch (error) {
        failures.push(new Error(`client event reporting: ${errorMessage(error)}`, { cause: error }))
      }
    }

    if (failures.length === 0 && resources.settled && clientEventReportingShutdown) {
      teardownComplete = true
    }
    throwFailures(failures, "Application teardown failed")
  }

  function requestTeardown(
    attempt: ResourceReleaseAttempt = {},
  ): Promise<void> {
    if (teardownComplete) return Promise.resolve()
    if (teardownOperation) return teardownOperation

    let resolvePhysical!: () => void
    let rejectPhysical!: (failure: unknown) => void
    const physicalOperation = new Promise<void>((resolve, reject) => {
      resolvePhysical = resolve
      rejectPhysical = reject
    })
    let operation!: Promise<void>
    operation = physicalOperation.finally(() => {
      if (teardownOperation === operation) teardownOperation = null
    })
    // Publish the joinable terminal operation before AbortController.abort()
    // or any release callback can synchronously re-enter teardown.
    teardownOperation = operation

    const closeFailures: unknown[] = []
    if (!resources.disposed) {
      try {
        resources.close(undefined, attempt)
      } catch (error) {
        closeFailures.push(error)
      }
    }

    void Promise.resolve()
      .then(() => runTeardown(closeFailures, attempt))
      .then(resolvePhysical, rejectPhysical)
    return operation
  }

  function teardown(
    attempt: ResourceReleaseAttempt = {},
  ): Promise<void> {
    if (publicTeardownOperation) return publicTeardownOperation

    let operation!: Promise<void>
    operation = Promise.resolve().then(async () => {
      const observation = observedTeardown
      if (observation) await observation

      const failures = retainedObservedTeardownFailures.splice(0)
      try {
        await requestTeardown(attempt)
      } catch (error) {
        failures.push(error)
      }
      throwFailures(failures, "Application observed teardown failed")
    }).finally(() => {
      if (publicTeardownOperation === operation) publicTeardownOperation = null
    })
    publicTeardownOperation = operation
    return operation
  }

  async function runStart(): Promise<void> {
    if (resources.disposed) throw new Error("Cannot start a closed application lifecycle")
    const attempt: ResourceReleaseAttempt = {}

    try {
      registerWindowResources(attempt)

      resources.acquire(
        () => runWithSynchronousScopeReleaseAttempt(
          attempt,
          () => app.mount(mountTarget),
        ),
        (_mounted, releaseAttempt = {}) => {
          runWithSynchronousScopeReleaseAttempt(
            releaseAttempt,
            () => app.unmount(),
          )
          started = false
        },
        "Vue application mount",
        attempt,
      )
      started = true
    } catch (setupError) {
      const cleanupErrors: unknown[] = []
      try {
        await requestTeardown(attempt)
      } catch (error) {
        cleanupErrors.push(error)
      }

      if (cleanupErrors.length > 0) {
        throw new AggregateError(
          [setupError, ...cleanupErrors],
          "Failed to start and roll back the application",
        )
      }
      throw setupError
    }
  }

  function start(): Promise<void> {
    if (started) return Promise.resolve()
    if (startOperation) return startOperation
    if (resources.disposed) {
      return Promise.reject(new Error("Cannot start a closed application lifecycle"))
    }

    const physicalOperation = Promise.resolve().then(runStart)
    let operation!: Promise<void>
    operation = physicalOperation.finally(() => {
      if (startOperation === operation) startOperation = null
    })
    startOperation = operation
    return operation
  }

  return {
    get closing() {
      return resources.disposed
    },
    get started() {
      return started
    },
    start,
    teardown,
    requestObservedTeardown,
  }
}
