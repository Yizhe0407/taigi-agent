import type { ResourceReleaseAttempt } from "@/lib/resource-owner"

export type AsyncReleaseAction = Readonly<{
  resource: string
  active: boolean
  operation: Promise<void> | null
  lastAttempt: ResourceReleaseAttempt | null
  failed: boolean
  failure: unknown
}>

export type AsyncReleaseOwner = Readonly<{
  actions: readonly AsyncReleaseAction[]
  settled: boolean
  claim(
    resource: string,
    release: (attempt: ResourceReleaseAttempt) => void | Promise<void>,
  ): AsyncReleaseAction
  start(
    action: AsyncReleaseAction,
    attempt: ResourceReleaseAttempt,
  ): Promise<void>
  settle(
    attempt: ResourceReleaseAttempt,
    candidates?: readonly AsyncReleaseAction[],
  ): Promise<void>
}>

type OwnedAsyncReleaseAction = {
  readonly resource: string
  readonly release: (attempt: ResourceReleaseAttempt) => void | Promise<void>
  active: boolean
  operation: Promise<void> | null
  lastAttempt: ResourceReleaseAttempt | null
  failed: boolean
  failure: unknown
  observer: Promise<void> | null
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

/**
 * Owns retryable asynchronous release claims.
 *
 * Every physical release is published on the claim before user code runs. The
 * published operation is always fulfilled, so callbacks may start cleanup
 * without creating an unhandled rejection; failure remains explicit state on
 * the authoritative claim. A failed exact attempt is never replayed, while a
 * later attempt first joins any older in-flight release and then retries only
 * the claims that still remain active.
 */
export function createAsyncReleaseOwner(label: string): AsyncReleaseOwner {
  const actions = new Set<OwnedAsyncReleaseAction>()

  const publicAction = (action: OwnedAsyncReleaseAction): AsyncReleaseAction => action

  const start = (
    publicClaim: AsyncReleaseAction,
    attempt: ResourceReleaseAttempt,
  ): Promise<void> => {
    const action = publicClaim as OwnedAsyncReleaseAction
    if (!action.active) return Promise.resolve()
    if (action.operation) return action.operation
    if (action.lastAttempt === attempt) return Promise.resolve()

    action.lastAttempt = attempt
    action.failed = false
    action.failure = undefined

    let resolvePhysical!: () => void
    const physical = new Promise<void>((resolve) => {
      resolvePhysical = resolve
    })
    let operation!: Promise<void>
    operation = physical.finally(() => {
      if (action.operation === operation) action.operation = null
    })
    action.operation = operation

    const finish = (
      result:
        | { readonly status: "released" }
        | { readonly status: "failed"; readonly failure: unknown },
    ): void => {
      if (result.status === "failed") {
        action.failed = true
        action.failure = result.failure
      } else {
        action.active = false
        actions.delete(action)
      }
      action.observer = null
      resolvePhysical()
    }

    let released: void | Promise<void>
    try {
      released = action.release(attempt)
    } catch (failure) {
      finish({ status: "failed", failure })
      return operation
    }

    // The observer is itself authoritative state on the claim. It always
    // fulfils after transferring success/failure into the published physical
    // operation, so neither synchronous callbacks nor asynchronous host
    // completions can create a detached rejected promise.
    action.observer = Promise.resolve(released).then(
      () => finish({ status: "released" }),
      failure => finish({ status: "failed", failure }),
    )
    return operation
  }

  const settleAction = async (
    action: OwnedAsyncReleaseAction,
    attempt: ResourceReleaseAttempt,
  ): Promise<
    | { readonly released: true }
    | { readonly released: false; readonly failure: unknown }
  > => {
    const olderOperation = action.operation
    if (olderOperation) await olderOperation
    if (!action.active) return { released: true }

    if (action.lastAttempt !== attempt) await start(action, attempt)
    if (!action.active) return { released: true }
    if (action.lastAttempt === attempt && action.failed) {
      return { released: false, failure: action.failure }
    }
    return {
      released: false,
      failure: new Error(`${action.resource} remains unresolved`),
    }
  }

  return {
    get actions() {
      return [...actions]
    },
    get settled() {
      return actions.size === 0
    },
    claim(resource, release) {
      const action: OwnedAsyncReleaseAction = {
        resource,
        release,
        active: true,
        operation: null,
        lastAttempt: null,
        failed: false,
        failure: undefined,
        observer: null,
      }
      actions.add(action)
      return publicAction(action)
    },
    start,
    async settle(attempt, candidates = [...actions]) {
      const ownedCandidates = candidates
        .map(candidate => candidate as OwnedAsyncReleaseAction)
        .filter(action => actions.has(action))
      const results = await Promise.all(
        ownedCandidates.map(async action => ({
          action,
          result: await settleAction(action, attempt),
        })),
      )
      const failures = results.flatMap(({ action, result }) =>
        result.released
          ? []
          : [new Error(`${action.resource}: ${errorMessage(result.failure)}`, {
              cause: result.failure,
            })],
      )

      if (failures.length === 1) throw failures[0]
      if (failures.length > 1) {
        throw new AggregateError(failures, `Failed to release ${label}`)
      }
    },
  }
}
