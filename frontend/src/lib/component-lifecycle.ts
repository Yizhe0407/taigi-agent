import { getCurrentInstance, onScopeDispose } from "vue"

import type { ResourceReleaseAttempt } from "@/lib/resource-owner"

type AsynchronousTeardownEntry = {
  readonly info: string
  readonly teardown: (attempt: ResourceReleaseAttempt) => Promise<void> | void
  readonly reportFailure: (failure: unknown) => void
  settled: boolean
  lastAttempt: ResourceReleaseAttempt | null
  failed: boolean
  lastFailure: unknown
  lastReportedAttempt: ResourceReleaseAttempt | null
  operation: Promise<void> | null
}

type AsynchronousTeardownReportingFailure = {
  readonly info: string
  readonly failure: unknown
}

type SynchronousTeardownEntry = {
  readonly info: string
  readonly teardown: (attempt: ResourceReleaseAttempt) => void
  readonly reportFailure: (failure: unknown) => void
  settled: boolean
  lastAttempt: ResourceReleaseAttempt | null
  failed: boolean
  lastFailure: unknown
  lastReportedAttempt: ResourceReleaseAttempt | null
  running: boolean
}

type SynchronousTeardownReportingFailure = {
  readonly info: string
  readonly entry: SynchronousTeardownEntry
  readonly attempt: ResourceReleaseAttempt
  readonly failure: unknown
}

// Failed component cleanup remains reachable independently of the disposed Vue
// scope. A later component/application teardown attempt retries the same closure
// instead of losing the only owner together with the component instance.
const retainedTeardowns = new Set<SynchronousTeardownEntry>()
const retainedSynchronousTeardownReportingFailures =
  new Set<SynchronousTeardownReportingFailure>()
const retainedAsynchronousTeardowns = new Set<AsynchronousTeardownEntry>()
const asynchronousTeardownObservations = new Set<Promise<void>>()
const retainedAsynchronousTeardownReportingFailures =
  new Set<AsynchronousTeardownReportingFailure>()
let activeScopeAttempt: ResourceReleaseAttempt | null = null

/** Return the release attempt currently governing synchronous Vue lifecycle work. */
export function currentSynchronousScopeReleaseAttempt(): ResourceReleaseAttempt | null {
  return activeScopeAttempt
}

/**
 * Run Vue mount/unmount work under one authoritative attempt so nested scope
 * disposal and component setup rollback cannot silently mint a second attempt.
 */
export function runWithSynchronousScopeReleaseAttempt<T>(
  attempt: ResourceReleaseAttempt,
  operation: () => T,
): T {
  const previousAttempt = activeScopeAttempt
  if (previousAttempt === null) activeScopeAttempt = attempt
  try {
    return operation()
  } finally {
    activeScopeAttempt = previousAttempt
  }
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

function reportUnhandledFailure(failure: unknown): void {
  if (typeof window !== "undefined" && typeof window.reportError === "function") {
    try {
      window.reportError(failure)
    } catch (reportingFailure) {
      throw new AggregateError(
        [failure, reportingFailure],
        "Component failure and the global error reporter both failed",
      )
    }
    return
  }
  throw failure
}

/**
 * Route teardown failures through the owning Vue application without reducing
 * cleanup debt to a console-only side effect.
 */
export function createComponentFailureReporter(info: string) {
  const componentInstance = getCurrentInstance()

  return (failure: unknown): void => {
    const handler = componentInstance?.appContext.config.errorHandler
    if (!handler) {
      reportUnhandledFailure(failure)
      return
    }

    try {
      handler(failure, componentInstance.proxy, info)
    } catch (handlerFailure) {
      reportUnhandledFailure(
        new AggregateError(
          [failure, handlerFailure],
          `${info} and its error handler both failed`,
        ),
      )
    }
  }
}

function attemptTeardown(
  entry: SynchronousTeardownEntry,
  attempt: ResourceReleaseAttempt,
): void {
  if (entry.settled || entry.running) return
  if (entry.lastAttempt === attempt) {
    if (entry.failed) throw entry.lastFailure
    return
  }

  entry.lastAttempt = attempt
  entry.failed = false
  entry.lastFailure = undefined
  entry.running = true
  try {
    runWithSynchronousScopeReleaseAttempt(
      attempt,
      () => entry.teardown(attempt),
    )
    entry.settled = true
    retainedTeardowns.delete(entry)
  } catch (failure) {
    entry.failed = true
    entry.lastFailure = failure
    retainedTeardowns.add(entry)
    throw failure
  } finally {
    entry.running = false
  }
}

function reportTeardownFailure(
  entry: SynchronousTeardownEntry,
  attempt: ResourceReleaseAttempt,
  failure: unknown,
): void {
  if (entry.lastReportedAttempt === attempt) return
  entry.lastReportedAttempt = attempt
  try {
    entry.reportFailure(failure)
  } catch (reportingFailure) {
    // Synchronous Vue disposal cannot be joined directly. Keep a separate,
    // application-owned delivery claim so a broken reporter neither interrupts
    // the remaining exact cleanups nor makes the combined failure disappear.
    retainedSynchronousTeardownReportingFailures.add({
      info: entry.info,
      entry,
      attempt,
      failure: reportingFailure,
    })
  }
}

function retryRetainedTeardowns(
  attempt: ResourceReleaseAttempt,
  excluded?: SynchronousTeardownEntry,
): void {
  for (const entry of [...retainedTeardowns]) {
    if (entry === excluded) continue
    try {
      attemptTeardown(entry, attempt)
    } catch (failure) {
      reportTeardownFailure(entry, attempt, failure)
    }
  }
}

/**
 * Retry all component teardown debt as one explicit application lifecycle
 * attempt. Failed exact claims remain retained for the next call.
 */
export function settleRetainedSynchronousTeardowns(
  requestedAttempt: ResourceReleaseAttempt = {},
): void {
  const attempt = activeScopeAttempt ?? requestedAttempt
  const failures: unknown[] = []
  for (const entry of [...retainedTeardowns]) {
    try {
      attemptTeardown(entry, attempt)
    } catch (failure) {
      failures.push(new Error(`${entry.info}: ${errorMessage(failure)}`, {
        cause: failure,
      }))
    }
  }

  if (failures.length === 1) throw failures[0]
  if (failures.length > 1) {
    throw new AggregateError(
      failures,
      "Failed to settle retained component teardown resources",
    )
  }
}

/**
 * Consume undelivered synchronous teardown reporting failures once and return
 * them together with the current unresolved cleanup debt. Cleanup ownership
 * itself remains retained until a later exact attempt succeeds.
 */
export function consumeRetainedSynchronousTeardownFailure(): unknown | null {
  const reportingFailures = [...retainedSynchronousTeardownReportingFailures]
  for (const failure of reportingFailures) {
    retainedSynchronousTeardownReportingFailures.delete(failure)
  }

  // A reporting failure already carries the cleanup failure from the exact
  // attempt handled by createComponentFailureReporter(). Do not duplicate that
  // same claim in this delivery, while still retaining it for a later retry.
  const representedEntries = new Set(
    reportingFailures.flatMap(({ entry, attempt }) =>
      entry.lastAttempt === attempt && entry.failed ? [entry] : []),
  )
  const failures = [
    ...reportingFailures.map(({ info, failure }) =>
      new Error(`${info}: ${errorMessage(failure)}`, { cause: failure })),
    ...[...retainedTeardowns]
      .filter(entry => !representedEntries.has(entry))
      .map((entry) => {
        const failure = entry.failed
          ? entry.lastFailure
          : new Error("cleanup remains unresolved")
        return new Error(`${entry.info}: ${errorMessage(failure)}`, {
          cause: failure,
        })
      }),
  ]

  if (failures.length === 0) return null
  if (failures.length === 1) return failures[0]
  return new AggregateError(
    failures,
    "Component teardown resources remain unresolved",
  )
}

/**
 * Own one retryable synchronous teardown beyond Vue scope disposal. Scope
 * disposal closes the component once, reports failure through Vue, and retains
 * only unresolved exact resources. The returned function is the explicit next
 * teardown attempt; other scope disposals and application shutdown also retry
 * previously retained debt.
 */
export function observeSynchronousScopeTeardown(
  info: string,
  teardown: (attempt: ResourceReleaseAttempt) => void,
): () => void {
  const entry: SynchronousTeardownEntry = {
    info,
    teardown,
    reportFailure: createComponentFailureReporter(info),
    settled: false,
    lastAttempt: null,
    failed: false,
    lastFailure: undefined,
    lastReportedAttempt: null,
    running: false,
  }

  onScopeDispose(() => {
    const attempt = activeScopeAttempt ?? {}
    const ownsAttempt = activeScopeAttempt === null
    if (ownsAttempt) activeScopeAttempt = attempt

    try {
      retryRetainedTeardowns(attempt, entry)
      try {
        attemptTeardown(entry, attempt)
      } catch (failure) {
        reportTeardownFailure(entry, attempt, failure)
      }
    } finally {
      if (ownsAttempt && activeScopeAttempt === attempt) {
        activeScopeAttempt = null
      }
    }
  })

  return () => attemptTeardown(entry, activeScopeAttempt ?? {})
}


function attemptAsynchronousTeardown(
  entry: AsynchronousTeardownEntry,
  attempt: ResourceReleaseAttempt,
): Promise<void> {
  if (entry.settled) return Promise.resolve()
  if (entry.operation) return entry.operation
  if (entry.lastAttempt === attempt) {
    return entry.failed
      ? Promise.reject(entry.lastFailure)
      : Promise.resolve()
  }

  entry.lastAttempt = attempt
  entry.failed = false
  entry.lastFailure = undefined
  retainedAsynchronousTeardowns.add(entry)

  let resolvePhysical!: () => void
  let rejectPhysical!: (failure: unknown) => void
  const physical = new Promise<void>((resolve, reject) => {
    resolvePhysical = resolve
    rejectPhysical = reject
  })
  let operation!: Promise<void>
  operation = physical.finally(() => {
    if (entry.operation === operation) entry.operation = null
  })
  // Publish the joinable physical operation before teardown can synchronously
  // re-enter through a host callback.
  entry.operation = operation

  const succeed = () => {
    entry.failed = false
    entry.lastFailure = undefined
    entry.settled = true
    retainedAsynchronousTeardowns.delete(entry)
    resolvePhysical()
  }
  const fail = (failure: unknown) => {
    entry.failed = true
    entry.lastFailure = failure
    rejectPhysical(failure)
  }

  let result: Promise<void> | void
  try {
    result = runWithSynchronousScopeReleaseAttempt(
      attempt,
      () => entry.teardown(attempt),
    )
  } catch (failure) {
    fail(failure)
    return operation
  }

  void Promise.resolve(result).then(succeed, fail)
  return operation
}

/** Join every asynchronous component teardown retained beyond its Vue scope. */
export async function settleRetainedAsynchronousTeardowns(
  requestedAttempt: ResourceReleaseAttempt = {},
): Promise<void> {
  const attempt = activeScopeAttempt ?? requestedAttempt
  // Scope-disposal observers own failure reporting that cannot be awaited by
  // Vue. Join them before taking the debt snapshot so a throwing reporter is
  // surfaced by this authoritative application lifecycle boundary rather than
  // becoming an unhandled rejection or disappearing with the component scope.
  await Promise.all([...asynchronousTeardownObservations])

  const entries = [...retainedAsynchronousTeardowns]
  const results = await Promise.allSettled(
    entries.map(entry => attemptAsynchronousTeardown(entry, attempt)),
  )
  const cleanupFailures = results.flatMap((result, index) =>
    result.status === "rejected"
      ? [new Error(`${entries[index]!.info}: ${errorMessage(result.reason)}`, {
          cause: result.reason,
        })]
      : [],
  )
  const reportingFailures = [...retainedAsynchronousTeardownReportingFailures]
  for (const failure of reportingFailures) {
    retainedAsynchronousTeardownReportingFailures.delete(failure)
  }

  const failures = [
    ...reportingFailures.map(({ info, failure }) =>
      new Error(`${info}: ${errorMessage(failure)}`, { cause: failure })),
    ...cleanupFailures,
  ]
  if (failures.length === 1) throw failures[0]
  if (failures.length > 1) {
    throw new AggregateError(
      failures,
      "Failed to settle retained asynchronous component teardown resources",
    )
  }
}

/**
 * Own one joinable asynchronous teardown after Vue discards the component
 * scope. The registry is authoritative until the exact cleanup succeeds; a
 * release attempt is never retried recursively under the same identity.
 */
export function observeAsynchronousScopeTeardown(
  info: string,
  teardown: (attempt: ResourceReleaseAttempt) => Promise<void> | void,
  reportFailure: (failure: unknown) => void = createComponentFailureReporter(info),
): () => Promise<void> {
  const entry: AsynchronousTeardownEntry = {
    info,
    teardown,
    reportFailure,
    settled: false,
    lastAttempt: null,
    failed: false,
    lastFailure: undefined,
    lastReportedAttempt: null,
    operation: null,
  }

  onScopeDispose(() => {
    const attempt = activeScopeAttempt ?? {}
    const operation = attemptAsynchronousTeardown(entry, attempt)
    let observation!: Promise<void>
    observation = operation.then(undefined, (failure) => {
      if (entry.lastReportedAttempt !== attempt) {
        entry.lastReportedAttempt = attempt
        try {
          entry.reportFailure(failure)
        } catch (reportingFailure) {
          retainedAsynchronousTeardownReportingFailures.add({
            info,
            failure: new AggregateError(
              [failure, reportingFailure],
              `${info} and its failure reporter both failed`,
            ),
          })
        }
      }
    }).finally(() => {
      asynchronousTeardownObservations.delete(observation)
    })
    asynchronousTeardownObservations.add(observation)
  })

  return () => attemptAsynchronousTeardown(
    entry,
    activeScopeAttempt ?? {},
  )
}
