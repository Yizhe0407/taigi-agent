import { QueryClient, VueQueryPlugin } from "@tanstack/vue-query"
import { flushPromises, mount } from "@vue/test-utils"
import { defineComponent } from "vue"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { fetchKiosk } from "@/features/route-planner/api/kiosk"
import { fetchMoovoStations } from "@/features/route-planner/api/moovo"
import { createRoutePlan } from "@/features/route-planner/api/route-plans"
import { useRoutePlanner } from "@/features/route-planner/composables/useRoutePlanner"
import type { RoutePlan } from "@/features/route-planner/types"
import { settleRetainedAsynchronousTeardowns } from "@/lib/component-lifecycle"

vi.mock("@/features/route-planner/api/kiosk", async (importOriginal) => {
  const actual = await importOriginal<
    typeof import("@/features/route-planner/api/kiosk")
  >()
  return { ...actual, fetchKiosk: vi.fn() }
})
vi.mock("@/features/route-planner/api/moovo", async (importOriginal) => {
  const actual = await importOriginal<
    typeof import("@/features/route-planner/api/moovo")
  >()
  return { ...actual, fetchMoovoStations: vi.fn() }
})
vi.mock("@/features/route-planner/api/route-plans", async (importOriginal) => {
  const actual = await importOriginal<
    typeof import("@/features/route-planner/api/route-plans")
  >()
  return { ...actual, createRoutePlan: vi.fn() }
})

type Deferred<T> = {
  promise: Promise<T>
  resolve: (value: T) => void
  reject: (reason: unknown) => void
}

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<T>((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, resolve, reject }
}

const FALLBACK_KIOSK_FOR_TEST = {
  name: "雲林科技大學",
  coordinates: [120.5355922, 23.6940747] as [number, number],
  direction: "回程" as const,
}

const routePlan: RoutePlan = {
  origin: { name: "origin", lat: 23.69, lng: 120.53 },
  destination: { name: "destination", lat: 23.71, lng: 120.55 },
  routes: [
    {
      id: "route-1",
      coordinates: [
        [120.53, 23.69],
        [120.55, 23.71],
      ],
      duration: 600,
      distance: 2000,
      transferCount: 0,
      legs: [],
    },
  ],
}

function mountPlanner(options: {
  queryClient?: QueryClient
  errorHandler?: (error: unknown) => void
} = {}) {
  const queryClient = options.queryClient ?? new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  let planner!: ReturnType<typeof useRoutePlanner>
  const wrapper = mount(
    defineComponent({
      setup() {
        planner = useRoutePlanner()
        return () => null
      },
    }),
    {
      global: {
        plugins: [[VueQueryPlugin, { queryClient }]],
        config: options.errorHandler
          ? { errorHandler: options.errorHandler }
          : undefined,
      },
    },
  )
  return { planner, queryClient, wrapper }
}

function collectFailureMessages(failure: unknown, messages: string[] = []): string[] {
  if (failure instanceof Error) messages.push(failure.message)
  else messages.push(String(failure))
  if (failure instanceof AggregateError) {
    for (const nested of failure.errors) collectFailureMessages(nested, messages)
  }
  if (failure instanceof Error && failure.cause !== undefined) {
    collectFailureMessages(failure.cause, messages)
  }
  return messages
}

describe("useRoutePlanner lifecycle", () => {
  beforeEach(() => {
    vi.mocked(fetchKiosk).mockResolvedValue({
      name: "kiosk",
      coordinates: [120.53, 23.69],
      direction: "回程",
    })
    vi.mocked(fetchMoovoStations).mockResolvedValue([])
    vi.mocked(createRoutePlan).mockReset()
  })

  afterEach(() => {
    vi.clearAllMocks()
    vi.unstubAllGlobals()
  })

  it("aborts a pending route and ignores a non-Abort rejection after disposal", async () => {
    const request = deferred<RoutePlan>()
    let ownedSignal: AbortSignal | undefined
    vi.mocked(createRoutePlan).mockImplementation((_destination, _date, signal) => {
      ownedSignal = signal
      return request.promise
    })
    const { planner, queryClient, wrapper } = mountPlanner()
    await flushPromises()

    planner.selectDestination([120.55, 23.71])
    const confirmation = planner.confirmDestination()
    await flushPromises()
    expect(ownedSignal?.aborted).toBe(false)
    expect(planner.isDestinationConfirmed.value).toBe(true)

    wrapper.unmount()
    expect(ownedSignal?.aborted).toBe(true)
    const teardown = planner.confirmDestination()
    expect(planner.confirmDestination()).toBe(teardown)

    let teardownSettled = false
    teardown.then(() => {
      teardownSettled = true
    })
    await flushPromises()
    expect(teardownSettled).toBe(false)

    request.reject(new Error("client represented cancellation generically"))
    await confirmation
    await teardown
    expect(teardownSettled).toBe(true)

    expect(planner.routePlan.value).toBeNull()
    expect(planner.routePlanError.value).toBe("")
    expect(planner.isDestinationConfirmed.value).toBe(true)
    queryClient.clear()
  })

  it("prevents retained public entry points from creating work or mutating state", async () => {
    vi.mocked(createRoutePlan).mockResolvedValue(routePlan)
    const { planner, queryClient, wrapper } = mountPlanner()
    await flushPromises()
    const moovoCalls = vi.mocked(fetchMoovoStations).mock.calls.length

    planner.selectDestination([120.55, 23.71])
    const destinationBeforeDispose = planner.destination.value
    wrapper.unmount()

    planner.selectDestination([120.56, 23.72])
    planner.rejectOutOfServiceArea()
    planner.resetDestination()
    planner.selectRoute("late")
    planner.loadMoovoStations()
    await planner.confirmDestination()
    await flushPromises()

    expect(planner.destination.value).toEqual(destinationBeforeDispose)
    expect(planner.routePlanError.value).toBe("")
    expect(createRoutePlan).not.toHaveBeenCalled()
    expect(fetchMoovoStations).toHaveBeenCalledTimes(moovoCalls)
    queryClient.clear()
  })

  it("joins concurrent MOOVO refresh callers and physical teardown after disposal", async () => {
    const request = deferred<Awaited<ReturnType<typeof fetchMoovoStations>>>()
    let ownedSignal: AbortSignal | undefined
    vi.mocked(fetchMoovoStations).mockImplementation((signal) => {
      ownedSignal = signal
      return request.promise
    })
    const { planner, queryClient, wrapper } = mountPlanner()
    await vi.waitFor(() => expect(ownedSignal).toBeDefined())

    const firstRefresh = planner.loadMoovoStations()
    const secondRefresh = planner.loadMoovoStations()
    expect(secondRefresh).toBe(firstRefresh)
    expect(ownedSignal?.aborted).toBe(false)

    wrapper.unmount()
    expect(ownedSignal?.aborted).toBe(true)

    let refreshSettled = false
    firstRefresh.then(() => {
      refreshSettled = true
    })
    await flushPromises()
    expect(refreshSettled).toBe(false)

    request.reject(new Error("late physical fetch rejection"))
    await firstRefresh
    expect(refreshSettled).toBe(true)

    await planner.loadMoovoStations()
    expect(fetchMoovoStations).toHaveBeenCalledOnce()
    expect(planner.moovoStationsError.value).toBe("")
    queryClient.clear()
  })

  it("keeps a replacement request authoritative when the aborted owner rejects late", async () => {
    const first = deferred<RoutePlan>()
    const second = deferred<RoutePlan>()
    const signals: AbortSignal[] = []
    vi.mocked(createRoutePlan)
      .mockImplementationOnce((_destination, _date, signal) => {
        signals.push(signal!)
        return first.promise
      })
      .mockImplementationOnce((_destination, _date, signal) => {
        signals.push(signal!)
        return second.promise
      })
    const { planner, queryClient, wrapper } = mountPlanner()
    await flushPromises()

    planner.selectDestination([120.55, 23.71])
    const firstConfirmation = planner.confirmDestination()
    await flushPromises()
    const secondConfirmation = planner.confirmDestination()
    await flushPromises()

    expect(signals[0]?.aborted).toBe(true)
    expect(signals[1]?.aborted).toBe(false)
    second.resolve(routePlan)
    await secondConfirmation
    first.reject(new Error("late first failure"))
    await firstConfirmation

    expect(planner.routePlan.value).toEqual(routePlan)
    expect(planner.selectedRoute.value?.id).toBe("route-1")
    expect(planner.routePlanError.value).toBe("")

    wrapper.unmount()
    queryClient.clear()
  })
  it("joins every superseded physical route request during disposal", async () => {
    const first = deferred<RoutePlan>()
    const second = deferred<RoutePlan>()
    vi.mocked(createRoutePlan)
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(second.promise)
    const { planner, queryClient, wrapper } = mountPlanner()
    await flushPromises()

    planner.selectDestination([120.55, 23.71])
    const firstConfirmation = planner.confirmDestination()
    await flushPromises()
    const secondConfirmation = planner.confirmDestination()
    await flushPromises()
    wrapper.unmount()

    const teardown = planner.confirmDestination()
    let settled = false
    teardown.then(() => {
      settled = true
    })

    second.reject(new Error("second canceled late"))
    await secondConfirmation
    await flushPromises()
    expect(settled).toBe(false)

    first.reject(new Error("first canceled later"))
    await firstConfirmation
    await teardown
    expect(settled).toBe(true)
    queryClient.clear()
  })

  it("cancels and physically joins the kiosk background query", async () => {
    const request = deferred<Awaited<ReturnType<typeof fetchKiosk>>>()
    let ownedSignal: AbortSignal | undefined
    vi.mocked(fetchKiosk).mockImplementation((signal) => {
      ownedSignal = signal
      return request.promise
    })
    const { planner, queryClient, wrapper } = mountPlanner()
    await vi.waitFor(() => expect(ownedSignal).toBeDefined())

    wrapper.unmount()
    expect(ownedSignal?.aborted).toBe(true)
    const teardown = planner.confirmDestination()
    let settled = false
    teardown.then(() => {
      settled = true
    })
    await flushPromises()
    expect(settled).toBe(false)

    request.reject(new Error("late kiosk cancellation"))
    await teardown
    expect(settled).toBe(true)
    expect(planner.kiosk.value).toEqual(FALLBACK_KIOSK_FOR_TEST)
    queryClient.clear()
  })

  it("publishes route cleanup before an AbortController constructor re-enters disposal", async () => {
    const NativeAbortController = AbortController
    let wrapperToUnmount: ReturnType<typeof mountPlanner>["wrapper"] | null = null
    let reenter = false
    class ReentrantAbortController extends NativeAbortController {
      constructor() {
        super()
        if (reenter) {
          reenter = false
          wrapperToUnmount?.unmount()
        }
      }
    }
    vi.stubGlobal("AbortController", ReentrantAbortController)
    const { planner, queryClient, wrapper } = mountPlanner()
    wrapperToUnmount = wrapper
    await flushPromises()

    planner.selectDestination([120.55, 23.71])
    reenter = true
    await planner.confirmDestination()
    await planner.confirmDestination()

    expect(createRoutePlan).not.toHaveBeenCalled()
    expect(planner.isPlanningRoute.value).toBe(false)
    queryClient.clear()
  })

  it("retains AbortController mutate-then-throw debt for the next public attempt", async () => {
    const NativeAbortController = AbortController
    const abortFailure = new Error("route abort failed after mutation")
    let routeSignal: AbortSignal | undefined
    let routeAbortCalls = 0
    class RetryableAbortController extends NativeAbortController {
      override abort(reason?: unknown): void {
        super.abort(reason)
        if (this.signal !== routeSignal) return
        routeAbortCalls += 1
        if (routeAbortCalls === 1) throw abortFailure
      }
    }
    vi.stubGlobal("AbortController", RetryableAbortController)
    const request = deferred<RoutePlan>()
    vi.mocked(createRoutePlan).mockImplementation((_destination, _date, signal) => {
      routeSignal = signal
      return request.promise
    })
    const handledErrors: unknown[] = []
    const { planner, queryClient, wrapper } = mountPlanner({
      errorHandler: error => handledErrors.push(error),
    })
    await flushPromises()

    planner.selectDestination([120.55, 23.71])
    const confirmation = planner.confirmDestination()
    await vi.waitFor(() => expect(routeSignal).toBeDefined())
    wrapper.unmount()
    expect(routeSignal?.aborted).toBe(true)
    expect(routeAbortCalls).toBe(1)

    request.reject(new Error("late aborted request"))
    await confirmation
    await vi.waitFor(() => expect(handledErrors).toHaveLength(1))
    expect(collectFailureMessages(handledErrors[0])).toContain(abortFailure.message)

    await planner.confirmDestination()
    expect(routeAbortCalls).toBe(2)
    queryClient.clear()
  })

  it("retries only the failed exact query cancellation action", async () => {
    const cleanupError = new Error("MOOVO cancellation failed")
    const handledErrors: unknown[] = []
    const { planner, queryClient, wrapper } = mountPlanner({
      errorHandler: error => handledErrors.push(error),
    })
    await flushPromises()

    const originalCancelQueries = queryClient.cancelQueries.bind(queryClient)
    const cancellationCalls: string[] = []
    vi.spyOn(queryClient, "cancelQueries").mockImplementation((filters) => {
      const resource = String(filters.queryKey?.[1])
      cancellationCalls.push(resource)
      if (resource === "moovo-stations" && cancellationCalls.filter(
        value => value === "moovo-stations",
      ).length === 1) {
        return Promise.reject(cleanupError)
      }
      return originalCancelQueries(filters)
    })

    wrapper.unmount()
    await vi.waitFor(() => expect(handledErrors).toHaveLength(1))
    expect(cancellationCalls.filter(value => value === "kiosk")).toHaveLength(1)
    expect(cancellationCalls.filter(value => value === "moovo-stations")).toHaveLength(1)

    await planner.confirmDestination()
    expect(cancellationCalls.filter(value => value === "kiosk")).toHaveLength(1)
    expect(cancellationCalls.filter(value => value === "moovo-stations")).toHaveLength(2)
    queryClient.clear()
  })

  it("prevents a disposed query generation from publishing into its successor", async () => {
    const firstKiosk = deferred<Awaited<ReturnType<typeof fetchKiosk>>>()
    const secondKiosk = deferred<Awaited<ReturnType<typeof fetchKiosk>>>()
    vi.mocked(fetchKiosk)
      .mockReturnValueOnce(firstKiosk.promise)
      .mockReturnValueOnce(secondKiosk.promise)
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    })

    const first = mountPlanner({ queryClient })
    await vi.waitFor(() => expect(fetchKiosk).toHaveBeenCalledTimes(1))
    first.wrapper.unmount()

    const second = mountPlanner({ queryClient })
    await vi.waitFor(() => expect(fetchKiosk).toHaveBeenCalledTimes(2))
    const successorKiosk = {
      name: "successor kiosk",
      coordinates: [120.54, 23.70] as [number, number],
      direction: "去程" as const,
    }
    secondKiosk.resolve(successorKiosk)
    await vi.waitFor(() => expect(second.planner.kiosk.value).toEqual(successorKiosk))

    firstKiosk.resolve({
      name: "stale kiosk",
      coordinates: [120.51, 23.68],
      direction: "回程",
    })
    await first.planner.confirmDestination()
    await flushPromises()
    expect(second.planner.kiosk.value).toEqual(successorKiosk)

    second.wrapper.unmount()
    await second.planner.confirmDestination()
    queryClient.clear()
  })

  it("retains cleanup and throwing reporter failures without an unhandled rejection", async () => {
    const cleanupError = new Error("route query cancellation failed")
    const handlerError = new Error("route teardown handler failed")
    const reporterError = new Error("route global reporter failed")
    const unhandled = vi.fn()
    window.addEventListener("unhandledrejection", unhandled)
    vi.stubGlobal("reportError", vi.fn(() => {
      throw reporterError
    }))

    const { queryClient, wrapper } = mountPlanner({
      errorHandler: () => {
        throw handlerError
      },
    })
    await flushPromises()
    const originalCancelQueries = queryClient.cancelQueries.bind(queryClient)
    vi.spyOn(queryClient, "cancelQueries")
      .mockRejectedValueOnce(cleanupError)
      .mockImplementation(filters => originalCancelQueries(filters))

    wrapper.unmount()
    await flushPromises()
    expect(unhandled).not.toHaveBeenCalled()

    let retainedFailure: unknown
    try {
      await settleRetainedAsynchronousTeardowns()
    } catch (failure) {
      retainedFailure = failure
    }
    const messages = collectFailureMessages(retainedFailure)
    expect(messages).toContain(cleanupError.message)
    expect(messages).toContain(handlerError.message)
    expect(messages).toContain(reporterError.message)
    expect(unhandled).not.toHaveBeenCalled()

    await settleRetainedAsynchronousTeardowns()
    window.removeEventListener("unhandledrejection", unhandled)
    queryClient.clear()
  })

  it("aggregates a teardown cleanup failure with a re-entrant physical setup failure", async () => {
    const NativeAbortController = AbortController
    const setupError = new Error("route controller construction failed")
    const cleanupError = new Error("kiosk cancellation failed")
    let wrapperToUnmount: ReturnType<typeof mountPlanner>["wrapper"] | null = null
    let failConstruction = false
    class ThrowingAbortController extends NativeAbortController {
      constructor() {
        super()
        if (failConstruction) {
          failConstruction = false
          wrapperToUnmount?.unmount()
          throw setupError
        }
      }
    }
    vi.stubGlobal("AbortController", ThrowingAbortController)
    const handledErrors: unknown[] = []
    const { planner, queryClient, wrapper } = mountPlanner({
      errorHandler: error => handledErrors.push(error),
    })
    wrapperToUnmount = wrapper
    await flushPromises()
    const originalCancelQueries = queryClient.cancelQueries.bind(queryClient)
    vi.spyOn(queryClient, "cancelQueries")
      .mockRejectedValueOnce(cleanupError)
      .mockImplementation(filters => originalCancelQueries(filters))

    planner.selectDestination([120.55, 23.71])
    failConstruction = true
    await planner.confirmDestination()
    await vi.waitFor(() => expect(handledErrors).toHaveLength(1))

    const messages = collectFailureMessages(handledErrors[0])
    expect(messages).toContain(setupError.message)
    expect(messages).toContain(cleanupError.message)
    await planner.confirmDestination()
    queryClient.clear()
  })

})
