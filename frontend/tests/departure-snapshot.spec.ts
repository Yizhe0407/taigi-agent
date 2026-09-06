import { QueryClient, VueQueryPlugin } from "@tanstack/vue-query"
import { mount } from "@vue/test-utils"
import { createApp, defineComponent } from "vue"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { useDepartureSnapshot } from "@/features/departures/composables/useDepartureSnapshot"
import { createApplicationLifecycle } from "@/lib/application-lifecycle"
import { settleRetainedSynchronousTeardowns } from "@/lib/component-lifecycle"
import { reportClientEvent } from "@/lib/report-client-event"

vi.mock("@/features/departures/api/departures", async (importOriginal) => {
  const actual = await importOriginal<
    typeof import("@/features/departures/api/departures")
  >()
  return {
    ...actual,
    fetchDeparturesHere: vi.fn(() => new Promise(() => {})),
  }
})

vi.mock("@/lib/report-client-event", () => ({
  reportClientEvent: vi.fn(),
}))

type EventCallback = ((event: Event) => void) | null
type MessageCallback = ((event: MessageEvent) => void) | null

class FakeEventSource {
  static instances: FakeEventSource[] = []
  static closeFailures: unknown[] = []
  static messageSetupFailures: unknown[] = []
  static messageCleanupFailures: unknown[] = []

  readonly openAssignments: EventCallback[] = []
  readonly errorAssignments: EventCallback[] = []
  readonly messageAssignments: MessageCallback[] = []

  private openCallback: EventCallback = null
  private errorCallback: EventCallback = null
  private messageCallback: MessageCallback = null

  readonly close = vi.fn(() => {
    const failure = FakeEventSource.closeFailures.shift()
    if (failure !== undefined) throw failure
  })

  constructor(public readonly url: string) {
    FakeEventSource.instances.push(this)
  }

  get onopen(): EventCallback {
    return this.openCallback
  }

  set onopen(callback: EventCallback) {
    this.openCallback = callback
    this.openAssignments.push(callback)
  }

  get onerror(): EventCallback {
    return this.errorCallback
  }

  set onerror(callback: EventCallback) {
    this.errorCallback = callback
    this.errorAssignments.push(callback)
  }

  get onmessage(): MessageCallback {
    return this.messageCallback
  }

  set onmessage(callback: MessageCallback) {
    this.messageCallback = callback
    this.messageAssignments.push(callback)
    const failure = callback === null
      ? FakeEventSource.messageCleanupFailures.shift()
      : FakeEventSource.messageSetupFailures.shift()
    if (failure !== undefined) throw failure
  }

  static reset(): void {
    FakeEventSource.instances = []
    FakeEventSource.closeFailures = []
    FakeEventSource.messageSetupFailures = []
    FakeEventSource.messageCleanupFailures = []
  }
}

function createQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
}

describe("useDepartureSnapshot", () => {
  beforeEach(() => {
    vi.stubGlobal("EventSource", FakeEventSource)
    vi.mocked(reportClientEvent).mockClear()
  })

  afterEach(() => {
    FakeEventSource.closeFailures = []
    FakeEventSource.messageSetupFailures = []
    FakeEventSource.messageCleanupFailures = []
    settleRetainedSynchronousTeardowns()
    FakeEventSource.reset()
    vi.unstubAllGlobals()
  })

  it("closes the SSE owner and permanently gates already queued callbacks", () => {
    const queryClient = createQueryClient()
    let result!: ReturnType<typeof useDepartureSnapshot>
    const wrapper = mount(
      defineComponent({
        setup() {
          result = useDepartureSnapshot()
          return () => null
        },
      }),
      {
        global: {
          plugins: [[VueQueryPlugin, { queryClient }]],
        },
      },
    )

    const source = FakeEventSource.instances[0]!
    const cachedOpen = source.onopen!
    const cachedError = source.onerror!
    const cachedMessage = source.onmessage!
    expect(result.refreshIntervalMs.value).toBe(15_000)

    wrapper.unmount()

    expect(source.close).toHaveBeenCalledOnce()
    expect(source.onopen).toBeNull()
    expect(source.onerror).toBeNull()
    expect(source.onmessage).toBeNull()

    cachedOpen(new Event("open"))
    cachedError(new Event("error"))
    cachedMessage(new MessageEvent("message", { data: "not json" }))
    cachedMessage(
      new MessageEvent("message", {
        data: JSON.stringify({
          stopName: "late",
          directionFilter: null,
          updatedAt: "late",
          summary: {
            availableCount: 0,
            notDepartedCount: 0,
            lastDepartedCount: 0,
            unknownCount: 0,
          },
          routes: [],
        }),
      }),
    )

    expect(result.refreshIntervalMs.value).toBe(15_000)
    expect(result.errorMessage.value).toBe("")
    expect(queryClient.getQueryData(["departures", "here"])).toBeUndefined()
    expect(reportClientEvent).not.toHaveBeenCalled()
    queryClient.clear()
  })

  it("retains a failed close claim and retries it only on the next teardown attempt", () => {
    const cleanupError = new Error("EventSource close failed after closing")
    FakeEventSource.closeFailures.push(cleanupError)
    const queryClient = createQueryClient()
    const handledErrors: unknown[] = []
    let result!: ReturnType<typeof useDepartureSnapshot>
    const wrapper = mount(
      defineComponent({
        setup() {
          result = useDepartureSnapshot()
          return () => null
        },
      }),
      {
        global: {
          plugins: [[VueQueryPlugin, { queryClient }]],
          config: {
            errorHandler: error => handledErrors.push(error),
          },
        },
      },
    )

    const source = FakeEventSource.instances[0]!
    const cachedOpen = source.onopen!
    const cachedError = source.onerror!
    const cachedMessage = source.onmessage!

    wrapper.unmount()

    expect(handledErrors).toEqual([cleanupError])
    expect(source.close).toHaveBeenCalledOnce()
    expect(source.onopen).toBeNull()
    expect(source.onerror).toBeNull()
    expect(source.onmessage).toBeNull()

    cachedOpen(new Event("open"))
    cachedError(new Event("error"))
    cachedMessage(new MessageEvent("message", { data: "not json" }))
    expect(result.refreshIntervalMs.value).toBe(15_000)
    expect(result.errorMessage.value).toBe("")
    expect(reportClientEvent).not.toHaveBeenCalled()

    expect(() => settleRetainedSynchronousTeardowns()).not.toThrow()
    expect(source.close).toHaveBeenCalledTimes(2)
    expect(source.openAssignments).toHaveLength(2)
    expect(source.errorAssignments).toHaveLength(2)
    expect(source.messageAssignments).toHaveLength(2)
    queryClient.clear()
  })

  it("fences a failed callback rollback until application teardown", async () => {
    const setupError = new Error("message callback failed after registration")
    const cleanupError = new Error("message callback cleanup failed after clearing")
    FakeEventSource.messageSetupFailures.push(setupError)
    FakeEventSource.messageCleanupFailures.push(cleanupError)
    const queryClient = createQueryClient()
    const Root = defineComponent({
      setup() {
        useDepartureSnapshot()
        return () => null
      },
    })
    const target = document.createElement("div")
    document.body.append(target)
    const app = createApp(Root)
    app.use(VueQueryPlugin, { queryClient })
    const handledErrors: unknown[] = []
    app.config.errorHandler = error => handledErrors.push(error)
    const lifecycle = createApplicationLifecycle({
      app,
      mountTarget: target,
      reportClientEvent: vi.fn(),
      shutdownClientEventReporting: vi.fn(async () => {}),
    })

    await lifecycle.start()

    const source = FakeEventSource.instances[0]!
    const cachedMessage = source.messageAssignments[0]!
    expect(handledErrors).toHaveLength(1)
    expect(handledErrors[0]).toMatchObject({
      name: "ResourceAcquisitionRollbackError",
      errors: [setupError, cleanupError],
    })
    expect(source.messageAssignments).toEqual([cachedMessage, null])
    expect(source.openAssignments).toHaveLength(2)
    expect(source.errorAssignments).toHaveLength(2)
    expect(source.close).toHaveBeenCalledOnce()

    cachedMessage(new MessageEvent("message", { data: "not json" }))
    expect(reportClientEvent).not.toHaveBeenCalled()

    await lifecycle.teardown()

    expect(source.messageAssignments).toEqual([cachedMessage, null, null])
    expect(source.openAssignments).toHaveLength(2)
    expect(source.errorAssignments).toHaveLength(2)
    expect(source.close).toHaveBeenCalledOnce()
    expect(handledErrors).toHaveLength(1)
    queryClient.clear()
    target.remove()
  })
})
