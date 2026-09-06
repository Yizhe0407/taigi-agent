export type ResourceClaim = {
  readonly active: boolean
  release(attempt?: ResourceReleaseAttempt): void
  transfer(): void
}

export type ResourceReleaseAttempt = object

export class ResourceOwnerClosedError extends Error {}

export class ResourceAcquisitionRollbackError extends AggregateError {
  constructor(errors: Iterable<unknown>, message: string) {
    super(errors, message)
    this.name = "ResourceAcquisitionRollbackError"
  }
}

export type AcquiredResource<T> = ResourceClaim & {
  readonly value: T
}

export type ResourceOwner = {
  readonly signal: AbortSignal
  readonly disposed: boolean
  readonly settled: boolean
  claim(
    resource: string,
    release: (attempt?: ResourceReleaseAttempt) => void,
  ): ResourceClaim
  acquire<T>(
    acquire: () => T,
    release: (
      value: T | undefined,
      attempt?: ResourceReleaseAttempt,
    ) => void,
    resource?: string,
    rollbackAttempt?: ResourceReleaseAttempt,
  ): AcquiredResource<T>
  close(reason?: unknown, attempt?: ResourceReleaseAttempt): void
  dispose(attempt?: ResourceReleaseAttempt): void
}

type OwnedClaim = {
  releaseResource: (attempt?: ResourceReleaseAttempt) => void
  active: boolean
  acquisitionPending: boolean
  releaseRequested: boolean
  releaseRequestedAttempt: ResourceReleaseAttempt | undefined
  lastReleaseAttempt: ResourceReleaseAttempt | undefined
  releaseInProgress: boolean
}

/**
 * Synchronous resource owner for browser handles and imperative registrations.
 *
 * Ownership is published before acquisition, disposal permanently closes the
 * acquisition gate before invoking user code, every release is attempted in
 * reverse order, and only failed exact releases remain owned for a later retry.
 */
export function createResourceOwner(label: string): ResourceOwner {
  const controller = new AbortController()
  const claims: OwnedClaim[] = []
  let disposalStarted = false
  let controllerReleased = false
  let controllerReleaseInProgress = false
  let lastControllerReleaseAttempt: ResourceReleaseAttempt | undefined
  let activeReleaseAttempt: ResourceReleaseAttempt | null = null

  const runReleaseAttempt = <T>(
    requestedAttempt: ResourceReleaseAttempt | undefined,
    release: (attempt: ResourceReleaseAttempt) => T,
  ): T => {
    const attempt = activeReleaseAttempt ?? requestedAttempt ?? {}
    const ownsAttempt = activeReleaseAttempt === null
    if (ownsAttempt) activeReleaseAttempt = attempt
    try {
      return release(attempt)
    } finally {
      if (ownsAttempt && activeReleaseAttempt === attempt) {
        activeReleaseAttempt = null
      }
    }
  }

  const removeClaim = (claim: OwnedClaim) => {
    const index = claims.indexOf(claim)
    if (index >= 0) claims.splice(index, 1)
  }

  const releaseClaim = (
    claim: OwnedClaim,
    attempt?: ResourceReleaseAttempt,
  ) => {
    if (!claim.active || claim.releaseInProgress) return
    if (attempt !== undefined && claim.lastReleaseAttempt === attempt) return
    if (claim.acquisitionPending) {
      if (!claim.releaseRequested) {
        claim.releaseRequested = true
        claim.releaseRequestedAttempt = attempt
      }
      return
    }

    claim.lastReleaseAttempt = attempt
    claim.releaseInProgress = true
    try {
      claim.releaseResource(attempt)
      claim.active = false
      removeClaim(claim)
    } finally {
      claim.releaseInProgress = false
    }
  }

  const transferClaim = (claim: OwnedClaim) => {
    if (!claim.active) return
    claim.active = false
    removeClaim(claim)
  }

  const publicClaim = (claim: OwnedClaim): ResourceClaim => ({
    get active() {
      return claim.active
    },
    release(attempt) {
      runReleaseAttempt(attempt, releaseAttempt => {
        releaseClaim(claim, releaseAttempt)
      })
    },
    transfer() {
      transferClaim(claim)
    },
  })

  const publishClaim = (
    resource: string,
    releaseResource: (attempt?: ResourceReleaseAttempt) => void,
    acquisitionPending: boolean,
  ): OwnedClaim => {
    if (disposalStarted) {
      throw new ResourceOwnerClosedError(`Cannot acquire ${resource} after disposing ${label}`)
    }
    const claim: OwnedClaim = {
      releaseResource,
      active: true,
      acquisitionPending,
      releaseRequested: false,
      releaseRequestedAttempt: undefined,
      lastReleaseAttempt: undefined,
      releaseInProgress: false,
    }
    claims.push(claim)
    return claim
  }

  const closeOwner = (
    reason?: unknown,
    attempt?: ResourceReleaseAttempt,
  ) => {
    disposalStarted = true
    if (controllerReleased || controllerReleaseInProgress) return
    if (
      attempt !== undefined
      && lastControllerReleaseAttempt === attempt
    ) return

    lastControllerReleaseAttempt = attempt
    controllerReleaseInProgress = true
    try {
      controller.abort(reason)
      controllerReleased = true
    } finally {
      controllerReleaseInProgress = false
    }
  }

  return {
    signal: controller.signal,
    get disposed() {
      return disposalStarted
    },
    get settled() {
      return claims.length === 0 && (!disposalStarted || controllerReleased)
    },
    claim(resource, release) {
      return publicClaim(publishClaim(resource, release, false))
    },
    acquire<T>(
      acquire: () => T,
      release: (
        value: T | undefined,
        attempt?: ResourceReleaseAttempt,
      ) => void,
      resource = "resource",
      rollbackAttempt?: ResourceReleaseAttempt,
    ): AcquiredResource<T> {
      let acquiredValue: T | undefined
      let hasAcquiredValue = false
      const claim = publishClaim(
        resource,
        attempt => release(
          hasAcquiredValue ? acquiredValue : undefined,
          attempt,
        ),
        true,
      )

      try {
        acquiredValue = acquire()
        hasAcquiredValue = true
      } catch (setupError) {
        claim.acquisitionPending = false
        try {
          releaseClaim(claim, rollbackAttempt)
        } catch (cleanupError) {
          throw new ResourceAcquisitionRollbackError(
            [setupError, cleanupError],
            `Failed to acquire and roll back ${resource}`,
          )
        }
        throw setupError
      }

      claim.acquisitionPending = false
      if (claim.releaseRequested || disposalStarted) {
        const closedError = new ResourceOwnerClosedError(
          `Cannot finish acquiring ${resource} after disposing ${label}`,
        )
        try {
          releaseClaim(
            claim,
            claim.releaseRequested
              ? claim.releaseRequestedAttempt
              : rollbackAttempt,
          )
        } catch (cleanupError) {
          throw new ResourceAcquisitionRollbackError(
            [closedError, cleanupError],
            `Failed to roll back ${resource} after disposing ${label}`,
          )
        }
        throw closedError
      }

      return {
        get active() {
          return claim.active
        },
        release(attempt) {
          runReleaseAttempt(attempt, releaseAttempt => {
            releaseClaim(claim, releaseAttempt)
          })
        },
        transfer() {
          transferClaim(claim)
        },
        value: acquiredValue as T,
      }
    },
    close(reason, attempt) {
      runReleaseAttempt(attempt, releaseAttempt => {
        closeOwner(reason, releaseAttempt)
      })
    },
    dispose(attempt) {
      runReleaseAttempt(attempt, releaseAttempt => {
        const failures: unknown[] = []

        try {
          closeOwner(undefined, releaseAttempt)
        } catch (error) {
          failures.push(error)
        }

        for (const claim of [...claims].reverse()) {
          try {
            releaseClaim(claim, releaseAttempt)
          } catch (error) {
            failures.push(error)
          }
        }

        if (failures.length === 1) throw failures[0]
        if (failures.length > 1) {
          throw new AggregateError(failures, `Failed to release ${label}`)
        }
      })
    },
  }
}
