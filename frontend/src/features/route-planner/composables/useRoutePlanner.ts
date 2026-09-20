import { useQuery, useQueryClient } from "@tanstack/vue-query"
import { computed, ref } from "vue"

import {
  createAsyncReleaseOwner,
  type AsyncReleaseAction,
} from "@/lib/async-release-owner"
import { observeAsynchronousScopeTeardown } from "@/lib/component-lifecycle"
import { UI_FALLBACK_MESSAGES } from "@/lib/api-messages"
import {
  createResourceOwner,
  type ResourceOwner,
  type ResourceReleaseAttempt,
} from "@/lib/resource-owner"
import { formatTaipeiHourMinute, parseTaipeiDateTimeInput } from "@/lib/time"
import { useNow } from "@/lib/useNow"

import { fetchKiosk } from "../api/kiosk"
import { fetchMoovoStations } from "../api/moovo"
import { createRoutePlan, RoutePlanApiError } from "../api/route-plans"
import { isInYunlinCounty } from "../geo/yunlin-service-area"
import type { KioskPlace, LngLat, MoovoStation, RoutePlan } from "../types"
import type { DepartureMode } from "./useScheduledTimeWheel"

const KIOSK_QUERY_KEY = ["route-planner", "kiosk"] as const
const MOOVO_QUERY_KEY = ["route-planner", "moovo-stations"] as const

const FALLBACK_KIOSK: KioskPlace = {
  name: "雲林科技大學",
  coordinates: [120.5355922, 23.6940747],
  direction: "回程",
}

let nextPlannerGeneration = 0

type QueryRequestKind = "kiosk" | "moovo"

type OwnedQueryRequest = {
  readonly kind: QueryRequestKind
  completion: Promise<void>
}

type OwnedPlannerOperation = {
  completion: Promise<void>
}

type RoutePlanOperation = {
  readonly generation: number
  resources: ResourceOwner | null
  readonly acquisitionSettled: Promise<void>
  readonly settleAcquisition: () => void
  releaseAction: AsyncReleaseAction
  releaseAttempt: ResourceReleaseAttempt | null
  completion: Promise<void>
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

function throwFailures(failures: unknown[], message: string): void {
  if (failures.length === 1) throw failures[0]
  if (failures.length > 1) throw new AggregateError(failures, message)
}

function staleQueryResultError(): DOMException {
  return new DOMException(
    "Route planner query generation is no longer authoritative",
    "AbortError",
  )
}

export function useRoutePlanner() {
  const queryClient = useQueryClient()
  const plannerGeneration = ++nextPlannerGeneration
  const kioskQueryKey = [...KIOSK_QUERY_KEY, plannerGeneration] as const
  const moovoQueryKey = [...MOOVO_QUERY_KEY, plannerGeneration] as const
  const releases = createAsyncReleaseOwner("route planner resources")
  const queryRequests = new Set<OwnedQueryRequest>()
  const plannerOperations = new Set<OwnedPlannerOperation>()
  const retainedPhysicalFailures: unknown[] = []

  const destination = ref<LngLat | null>(null)
  const isDestinationConfirmed = ref(false)
  const routePlan = ref<RoutePlan | null>(null)
  const routePlanError = ref("")
  const routePlanErrorKind = ref<"no-service" | "generic">("generic")
  const selectedRouteId = ref<string | null>(null)
  const departureMode = ref<DepartureMode>("now")
  const scheduledDateTime = ref("")
  const isPlanningRoute = ref(false)

  let activeRoutePlan: RoutePlanOperation | null = null
  let routePlanGeneration = 0
  let moovoRefreshTask: Promise<void> | null = null
  let disposed = false
  let disposedKiosk: KioskPlace = { ...FALLBACK_KIOSK }
  let disposedMoovoStations: MoovoStation[] = []
  let disposedMoovoLoading = false
  let disposedMoovoError = ""

  const ownQueryRequest = <T>(
    kind: QueryRequestKind,
    signal: AbortSignal,
    request: (signal: AbortSignal) => Promise<T>,
  ): Promise<T> => {
    const physical = Promise.resolve()
      .then(() => request(signal))
      .then((value) => {
        if (disposed) throw staleQueryResultError()
        return value
      })
    const owned: OwnedQueryRequest = {
      kind,
      completion: Promise.resolve(),
    }
    owned.completion = physical.then(
      () => {
        queryRequests.delete(owned)
      },
      () => {
        queryRequests.delete(owned)
      },
    )
    queryRequests.add(owned)
    return physical
  }

  const joinQueryRequests = async (kind?: QueryRequestKind): Promise<void> => {
    for (;;) {
      const requests = [...queryRequests]
        .filter(request => kind === undefined || request.kind === kind)
      if (requests.length === 0) return
      await Promise.all(requests.map(request => request.completion))
    }
  }

  const joinPlannerOperations = async (): Promise<void> => {
    for (;;) {
      const operations = [...plannerOperations]
      if (operations.length === 0) return
      await Promise.all(operations.map(operation => operation.completion))
    }
  }

  releases.claim(
    "kiosk query cancellation",
    () => queryClient.cancelQueries({ queryKey: kioskQueryKey, exact: true }),
  )
  releases.claim(
    "MOOVO query cancellation",
    () => queryClient.cancelQueries({ queryKey: moovoQueryKey, exact: true }),
  )
  const kioskCacheRemoval = releases.claim(
    "kiosk query cache",
    () => queryClient.removeQueries({ queryKey: kioskQueryKey, exact: true }),
  )
  const moovoCacheRemoval = releases.claim(
    "MOOVO query cache",
    () => queryClient.removeQueries({ queryKey: moovoQueryKey, exact: true }),
  )

  const queryCacheActions = [kioskCacheRemoval, moovoCacheRemoval] as const

  let kioskQuery!: ReturnType<typeof useQuery<KioskPlace>>
  let moovoQuery!: ReturnType<typeof useQuery<MoovoStation[]>>

  const kiosk = computed<KioskPlace>(() =>
    disposed
      ? disposedKiosk
      : kioskQuery?.data.value ?? FALLBACK_KIOSK,
  )
  const moovoStations = computed<MoovoStation[]>(() =>
    disposed
      ? disposedMoovoStations
      : moovoQuery?.data.value ?? [],
  )
  const isLoadingMoovoStations = computed(() =>
    disposed ? disposedMoovoLoading : moovoQuery?.isLoading.value ?? false,
  )
  const moovoStationsError = computed(() => {
    if (disposed) return disposedMoovoError
    return moovoQuery?.error.value ? UI_FALLBACK_MESSAGES.moovoUnavailable : ""
  })

  const releaseRouteOperation = (
    operation: RoutePlanOperation,
    attempt: ResourceReleaseAttempt,
  ): void => {
    if (operation.releaseAttempt === null) operation.releaseAttempt = attempt
    releases.start(operation.releaseAction, operation.releaseAttempt)
  }

  const retireActiveRoutePlan = (
    attempt: ResourceReleaseAttempt = {},
  ): void => {
    const operation = activeRoutePlan
    if (!operation) return

    // Authority is revoked before cancellation. Even a mutate-then-throw abort
    // can therefore never publish into a successor route generation.
    if (activeRoutePlan === operation) {
      activeRoutePlan = null
      isPlanningRoute.value = false
    }
    releaseRouteOperation(operation, attempt)
  }

  const teardownRoutePlanner = async (
    attempt: ResourceReleaseAttempt,
  ): Promise<void> => {
    if (!disposed) {
      disposedKiosk = kiosk.value
      disposedMoovoStations = [...moovoStations.value]
      disposedMoovoLoading = isLoadingMoovoStations.value
      disposedMoovoError = moovoStationsError.value
      disposed = true
    }

    retireActiveRoutePlan(attempt)
    const failures: unknown[] = []
    const routeActions = releases.actions.filter(
      action => !queryCacheActions.includes(action as typeof queryCacheActions[number]),
    )

    // Publish every cancellation before awaiting any one of them. Physical
    // requests and route continuations remain independently owned and are then
    // joined to completion instead of assuming query cancellation is a join.
    for (const action of routeActions) releases.start(action, attempt)
    const releaseSettlement = releases.settle(attempt, routeActions)
    const workSettlement = Promise.all([
      joinQueryRequests(),
      joinPlannerOperations(),
    ])
    const [releaseResult, workResult] = await Promise.allSettled([
      releaseSettlement,
      workSettlement,
    ])
    if (releaseResult.status === "rejected") failures.push(releaseResult.reason)
    if (workResult.status === "rejected") failures.push(workResult.reason)

    const physicalFailures = retainedPhysicalFailures.splice(0)
    failures.push(...physicalFailures.map(failure => new Error(
      `route planner physical operation: ${errorMessage(failure)}`,
      { cause: failure },
    )))

    try {
      await releases.settle(attempt, queryCacheActions)
    } catch (failure) {
      failures.push(failure)
    }

    throwFailures(failures, "Failed to release route planner resources")
  }

  const settleScopeTeardown = observeAsynchronousScopeTeardown(
    "Route planner asynchronous teardown",
    teardownRoutePlanner,
  )

  // Query ownership and scope teardown are both published before useQuery may
  // schedule a query function. Each component generation has distinct keys, and
  // the query function itself rejects results after its generation closes.
  kioskQuery = useQuery<KioskPlace>({
    queryKey: kioskQueryKey,
    queryFn: ({ signal }) => ownQueryRequest("kiosk", signal, fetchKiosk),
    initialData: { ...FALLBACK_KIOSK },
    retry: 1,
  })
  moovoQuery = useQuery<MoovoStation[]>({
    queryKey: moovoQueryKey,
    queryFn: ({ signal }) => ownQueryRequest("moovo", signal, fetchMoovoStations),
    retry: false,
  })

  const { now } = useNow(10_000)
  const nowLabel = computed(() => formatTaipeiHourMinute(now.value))

  const selectedRoute = computed(() => {
    if (!routePlan.value) return null
    return (
      routePlan.value.routes.find(route => route.id === selectedRouteId.value) ??
      routePlan.value.routes[0] ??
      null
    )
  })

  function clearRoutePlan(attempt: ResourceReleaseAttempt = {}): void {
    retireActiveRoutePlan(attempt)
    routePlan.value = null
    routePlanError.value = ""
    routePlanErrorKind.value = "generic"
    selectedRouteId.value = null
  }

  function rejectOutOfServiceArea(): void {
    if (disposed) return
    clearRoutePlan()
    routePlanError.value = "目的地超出範圍，請選擇雲林縣內的地點"
    routePlanErrorKind.value = "generic"
  }

  function selectDestination(coordinates: LngLat): void {
    if (disposed) return
    if (!isInYunlinCounty(coordinates)) {
      rejectOutOfServiceArea()
      return
    }
    clearRoutePlan()
    destination.value = coordinates
    isDestinationConfirmed.value = false
  }

  function scheduledDepartureDate(): Date | undefined {
    if (departureMode.value === "now") return undefined
    return parseTaipeiDateTimeInput(scheduledDateTime.value)
  }

  function confirmDestination(): Promise<void> {
    if (disposed) return settleScopeTeardown()
    if (!destination.value) return Promise.resolve()

    const currentDestination = [...destination.value] as LngLat
    const departureDate = scheduledDepartureDate()
    const supersessionAttempt: ResourceReleaseAttempt = {}

    clearRoutePlan(supersessionAttempt)
    isDestinationConfirmed.value = true
    isPlanningRoute.value = true

    let settleAcquisition!: () => void
    const acquisitionSettled = new Promise<void>((resolve) => {
      settleAcquisition = resolve
    })
    const operation: RoutePlanOperation = {
      generation: ++routePlanGeneration,
      resources: null,
      acquisitionSettled,
      settleAcquisition,
      releaseAction: null as unknown as AsyncReleaseAction,
      releaseAttempt: null,
      completion: Promise.resolve(),
    }

    operation.releaseAction = releases.claim(
      `route request generation ${operation.generation}`,
      (attempt) => {
        const release = (): void => {
          const resources = operation.resources
          if (!resources) return
          resources.dispose(attempt)
          if (!resources.settled) {
            throw new Error(
              `route request generation ${operation.generation} remains unsettled`,
            )
          }
        }

        // Once acquisition has published the exact owner, cancellation remains
        // synchronous at scope disposal. Re-entrant disposal before publication
        // instead joins the already-owned acquisition placeholder.
        if (operation.resources) {
          release()
          return
        }
        return operation.acquisitionSettled.then(release)
      },
    )
    activeRoutePlan = operation

    const isAuthoritative = (): boolean =>
      !disposed
      && activeRoutePlan === operation
      && operation.resources?.signal.aborted === false

    const physical = Promise.resolve().then(async () => {
      try {
        operation.resources = createResourceOwner(
          `route request generation ${operation.generation}`,
        )
      } catch (failure) {
        if (disposed) retainedPhysicalFailures.push(failure)
        if (activeRoutePlan === operation && !disposed) {
          routePlanErrorKind.value = "generic"
          routePlanError.value = "路線規劃失敗，請稍後再試"
          isDestinationConfirmed.value = false
        }
        return
      } finally {
        operation.settleAcquisition()
      }

      if (!isAuthoritative()) return

      try {
        const plan = await createRoutePlan(
          currentDestination,
          departureDate,
          operation.resources.signal,
        )
        if (!isAuthoritative()) return

        routePlan.value = plan
        selectedRouteId.value = plan.routes[0]?.id ?? null
        if (!selectedRouteId.value) {
          routePlanError.value = "路線規劃完成，但沒有可顯示的候選路線"
          routePlanErrorKind.value = "generic"
          isDestinationConfirmed.value = false
        }
      } catch (failure) {
        if (!isAuthoritative()) return

        if (failure instanceof RoutePlanApiError) {
          routePlanErrorKind.value = failure.status === 404 ? "no-service" : "generic"
          routePlanError.value =
            failure.status !== null && failure.status >= 500
              ? UI_FALLBACK_MESSAGES.routePlanUnavailable
              : failure.message
        } else {
          routePlanErrorKind.value = "generic"
          routePlanError.value = "路線規劃失敗，請稍後再試"
        }
        isDestinationConfirmed.value = false
      }
    })

    let completion!: Promise<void>
    completion = physical
      .then(undefined, (failure) => {
        if (disposed) retainedPhysicalFailures.push(failure)
      })
      .then(async () => {
        if (activeRoutePlan === operation) {
          activeRoutePlan = null
          if (!disposed) isPlanningRoute.value = false
        }
        const releaseAttempt = operation.releaseAttempt ?? {}
        operation.releaseAttempt = releaseAttempt
        try {
          await releases.settle(releaseAttempt, [operation.releaseAction])
        } catch {
          // The exact failed release remains authoritative debt on `releases`.
          // Scope/application teardown performs the next distinct retry.
        }
      })
      .finally(() => {
        plannerOperations.delete(operation)
      })
    operation.completion = completion
    plannerOperations.add(operation)
    return completion
  }

  function resetDestination(): void {
    if (disposed) return
    clearRoutePlan()
    destination.value = null
    isDestinationConfirmed.value = false
    departureMode.value = "now"
    scheduledDateTime.value = ""
  }

  function selectRoute(routeId: string): void {
    if (disposed) return
    selectedRouteId.value = routeId
  }

  function loadMoovoStations(): Promise<void> {
    if (disposed) return settleScopeTeardown()
    if (moovoRefreshTask) return moovoRefreshTask

    const owned: OwnedPlannerOperation = {
      completion: Promise.resolve(),
    }
    const physical = Promise.resolve().then(async () => {
      if (!disposed) {
        await moovoQuery.refetch({ cancelRefetch: false })
      }
      await joinQueryRequests("moovo")
    })
    let task!: Promise<void>
    task = physical
      .then(
        () => undefined,
        () => undefined,
      )
      .finally(() => {
        plannerOperations.delete(owned)
        if (moovoRefreshTask === task) moovoRefreshTask = null
      })
    owned.completion = task
    plannerOperations.add(owned)
    moovoRefreshTask = task
    return task
  }

  return {
    kiosk,
    destination,
    isDestinationConfirmed,
    isPlanningRoute,
    routePlan,
    routePlanError,
    routePlanErrorKind,
    selectedRoute,
    moovoStations,
    isLoadingMoovoStations,
    moovoStationsError,
    departureMode,
    scheduledDateTime,
    nowLabel,
    loadMoovoStations,
    selectDestination,
    rejectOutOfServiceArea,
    confirmDestination,
    resetDestination,
    selectRoute,
  }
}
