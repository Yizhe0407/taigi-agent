import type { Map as MapLibreMap } from "maplibre-gl"
import { watch, type Ref } from "vue"

import { observeSynchronousScopeTeardown } from "@/lib/component-lifecycle"
import {
  createResourceOwner,
  type AcquiredResource,
  type ResourceOwner,
  type ResourceReleaseAttempt,
} from "@/lib/resource-owner"

export type MapResourceOwner = Readonly<{
  signal: AbortSignal
}> & {
  acquire<T>(
    acquire: () => T,
    release: (value: T | undefined) => void,
    resource?: string,
  ): AcquiredResource<T>
}

export type LayerSetup = (
  map: MapLibreMap,
  owner: MapResourceOwner,
  fail: (error: unknown) => never,
) => void

export type MapLayerLifecycle = {
  isActive(map?: MapLibreMap): boolean
  fail(error: unknown, map: MapLibreMap): never
}

/**
 * Own one MapLibre layer setup for the current loaded map. The composable keeps
 * the owner reachable before setup starts, so partial-construction rollback
 * failures can be retried on scope disposal instead of becoming orphaned.
 */
export function useMapLayer(
  map: Ref<MapLibreMap | null>,
  isLoaded: Ref<boolean>,
  label: string,
  setup: LayerSetup,
): MapLayerLifecycle {
  let activeMap: MapLibreMap | null = null
  let activeOwner: ResourceOwner | null = null
  let disposed = false

  const teardownOwner = (
    owner: ResourceOwner,
    attempt: ResourceReleaseAttempt,
    unresolvedFailureAlreadyRepresented = false,
  ) => {
    owner.dispose(attempt)
    if (activeOwner === owner && owner.settled) {
      activeOwner = null
      activeMap = null
      return
    }
    if (!owner.settled && !unresolvedFailureAlreadyRepresented) {
      throw new Error(`${label} cleanup remains unresolved`)
    }
  }

  const teardown = (attempt: ResourceReleaseAttempt) => {
    const owner = activeOwner
    if (!owner) {
      activeMap = null
      return
    }
    teardownOwner(owner, attempt)
  }

  const failOwnedGeneration = (
    error: unknown,
    owner: ResourceOwner,
    mapInstance: MapLibreMap,
    attempt: ResourceReleaseAttempt,
  ): never => {
    // A callback may return only after synchronous teardown/replacement. Never
    // let that predecessor callback retry its debt or dispose its successor.
    if (
      disposed
      || owner.disposed
      || activeOwner !== owner
      || activeMap !== mapInstance
    ) {
      throw error
    }

    disposed = true
    try {
      teardownOwner(owner, attempt, true)
    } catch (cleanupError) {
      throw new AggregateError(
        [error, cleanupError],
        `${label} failed and could not be fully released`,
      )
    }
    throw error
  }

  observeSynchronousScopeTeardown(`${label} teardown`, (attempt) => {
    disposed = true
    teardown(attempt)
  })

  watch(
    [map, isLoaded],
    ([mapInstance, loaded]) => {
      if (disposed) return
      const attempt: ResourceReleaseAttempt = {}
      if (!mapInstance || !loaded) {
        try {
          teardown(attempt)
        } catch (error) {
          disposed = true
          throw error
        }
        return
      }
      if (activeMap === mapInstance) return

      try {
        teardown(attempt)
      } catch (error) {
        disposed = true
        throw error
      }

      const owner = createResourceOwner(label)
      const setupOwner: MapResourceOwner = {
        signal: owner.signal,
        acquire(acquire, release, resource) {
          return owner.acquire(acquire, release, resource, attempt)
        },
      }
      activeMap = mapInstance
      activeOwner = owner
      try {
        setup(
          mapInstance,
          setupOwner,
          error => failOwnedGeneration(error, owner, mapInstance, {}),
        )
      } catch (error) {
        if (
          disposed
          || owner.disposed
          || activeOwner !== owner
          || activeMap !== mapInstance
        ) {
          throw error
        }
        failOwnedGeneration(error, owner, mapInstance, attempt)
      }
    },
    { immediate: true },
  )

  return {
    isActive(candidate?: MapLibreMap) {
      return (
        !disposed &&
        activeOwner !== null &&
        !activeOwner.signal.aborted &&
        (candidate === undefined || candidate === activeMap)
      )
    },
    fail(error, mapInstance) {
      const owner = activeOwner
      if (
        disposed
        || !owner
        || owner.disposed
        || activeMap !== mapInstance
      ) {
        throw error
      }
      return failOwnedGeneration(error, owner, mapInstance, {})
    },
  }
}
