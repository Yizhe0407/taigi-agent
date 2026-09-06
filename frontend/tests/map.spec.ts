import { mount } from "@vue/test-utils"
import type { MapOptions, ProjectionSpecification } from "maplibre-gl"
import { createApp } from "vue"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import MapComponent from "@/components/ui/map/Map.vue"
import { createApplicationLifecycle } from "@/lib/application-lifecycle"

const mapHarness = vi.hoisted(() => {
  type Handler = (...args: unknown[]) => void

  class MapInstance {
    static instances: MapInstance[] = []
    static failingOnEvent: string | null = null
    static failingOffEvent: string | null = null

    readonly handlers = new Map<string, Handler>()
    readonly options: MapOptions
    readonly container = document.createElement("div")
    readonly on = vi.fn((event: string, handler: Handler) => {
      this.handlers.set(event, handler)
      if (MapInstance.failingOnEvent === event) {
        throw new Error(`${event} registration failed`)
      }
      return this
    })
    readonly off = vi.fn((event: string, handler: Handler) => {
      if (this.handlers.get(event) === handler) this.handlers.delete(event)
      if (MapInstance.failingOffEvent === event) {
        MapInstance.failingOffEvent = null
        throw new Error(`${event} removal failed`)
      }
      return this
    })
    readonly remove = vi.fn()
    readonly getContainer = vi.fn(() => this.container)
    readonly getStyle = vi.fn(() => ({ layers: [] }))
    readonly setLayoutProperty = vi.fn()
    readonly setProjection = vi.fn()
    readonly setStyle = vi.fn()
    readonly isMoving = vi.fn(() => false)
    readonly jumpTo = vi.fn()
    readonly getCenter = vi.fn(() => ({ lng: 120.5, lat: 23.7 }))
    readonly getZoom = vi.fn(() => 12)
    readonly getBearing = vi.fn(() => 0)
    readonly getPitch = vi.fn(() => 0)

    constructor(options: MapOptions) {
      this.options = options
      const attribution = document.createElement("div")
      attribution.classList.add("maplibregl-ctrl-attrib", "maplibregl-compact-show")
      this.container.append(attribution)
      MapInstance.instances.push(this)
    }
  }

  return { Map: MapInstance }
})

vi.mock("maplibre-gl", () => ({
  default: { Map: mapHarness.Map },
}))

type ScheduledTimer = {
  callback: () => void
  delay: number | undefined
}

function installTimerHarness() {
  let nextId = 1
  const scheduled = new Map<number, ScheduledTimer>()
  const setTimeout = vi.spyOn(window, "setTimeout").mockImplementation(
    ((callback: TimerHandler, delay?: number) => {
      const id = nextId++
      scheduled.set(id, { callback: callback as () => void, delay })
      return id
    }) as typeof window.setTimeout,
  )
  const clearTimeout = vi.spyOn(globalThis, "clearTimeout").mockImplementation(
    ((id: number | undefined) => {
      if (id !== undefined) scheduled.delete(id)
    }) as typeof globalThis.clearTimeout,
  )
  return { scheduled, setTimeout, clearTimeout }
}

function installThemeHosts() {
  const observer = {
    observe: vi.fn(),
    disconnect: vi.fn(),
  }
  class MutationObserver {
    constructor(_callback: MutationCallback) {}

    readonly observe = observer.observe
    readonly disconnect = observer.disconnect
    readonly takeRecords = vi.fn(() => [])
  }
  const mediaQuery = {
    matches: false,
    media: "(prefers-color-scheme: dark)",
    onchange: null,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    addListener: vi.fn(),
    removeListener: vi.fn(),
    dispatchEvent: vi.fn(() => true),
  } as unknown as MediaQueryList
  vi.stubGlobal("MutationObserver", MutationObserver)
  vi.stubGlobal("matchMedia", vi.fn(() => mediaQuery))
  return { observer, mediaQuery }
}

beforeEach(() => {
  document.documentElement.classList.remove("dark", "light")
  mapHarness.Map.instances = []
  mapHarness.Map.failingOnEvent = null
  mapHarness.Map.failingOffEvent = null
  installThemeHosts()
})

afterEach(() => {
  document.documentElement.classList.remove("dark", "light")
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe("Map lifecycle", () => {
  it("does not retry a failed listener rollback inside the same setup attempt", async () => {
    mapHarness.Map.failingOnEvent = "styledata"
    mapHarness.Map.failingOffEvent = "styledata"
    const handledErrors: unknown[] = []
    const target = document.createElement("div")
    document.body.append(target)
    const app = createApp(MapComponent, { theme: "light" })
    app.config.errorHandler = error => handledErrors.push(error)
    const lifecycle = createApplicationLifecycle({
      app,
      mountTarget: target,
      reportClientEvent: vi.fn(),
      shutdownClientEventReporting: vi.fn(async () => {}),
    })

    await lifecycle.start()
    const map = mapHarness.Map.instances[0]!
    const styleOffCalls = () => map.off.mock.calls.filter(
      ([event]) => event === "styledata",
    )

    expect(handledErrors).toHaveLength(1)
    expect(handledErrors[0]).toMatchObject({
      message: "Failed to acquire and roll back MapLibre style listener",
    })
    expect(styleOffCalls()).toHaveLength(1)
    expect(map.remove).toHaveBeenCalledOnce()

    await lifecycle.teardown()
    expect(styleOffCalls()).toHaveLength(2)
    expect(map.remove).toHaveBeenCalledOnce()
    target.remove()
  })

  it("retains and retries the exact style timer when unmount cleanup fails", () => {
    const cleanupError = new Error("clear style timer failed")
    const handledErrors: unknown[] = []
    const projection = { type: "mercator" } as ProjectionSpecification
    const wrapper = mount(MapComponent, {
      props: { theme: "light", projection },
      global: {
        config: {
          errorHandler: error => handledErrors.push(error),
        },
      },
    })
    const map = mapHarness.Map.instances[0]!
    const styleDataHandler = map.handlers.get("styledata")!
    const timers = installTimerHarness()
    timers.clearTimeout.mockImplementationOnce(() => {
      throw cleanupError
    })

    styleDataHandler()
    expect(timers.setTimeout).toHaveBeenCalledOnce()
    expect(timers.scheduled.get(1)?.delay).toBe(100)
    const cachedTimerCallback = timers.scheduled.get(1)!.callback
    const settleMapTeardown = (wrapper.vm as unknown as {
      settleMapTeardown: () => void
    }).settleMapTeardown

    wrapper.unmount()

    expect(handledErrors).toEqual([cleanupError])
    expect(timers.clearTimeout.mock.calls.map(([id]) => id)).toEqual([1])
    expect(timers.setTimeout).toHaveBeenCalledOnce()
    expect(map.remove).toHaveBeenCalledOnce()

    expect(() => settleMapTeardown()).not.toThrow()
    expect(timers.clearTimeout.mock.calls.map(([id]) => id)).toEqual([1, 1])
    expect(timers.setTimeout).toHaveBeenCalledOnce()
    expect(map.remove).toHaveBeenCalledOnce()

    cachedTimerCallback()
    styleDataHandler()
    expect(map.setProjection).not.toHaveBeenCalled()
    expect(map.setLayoutProperty).not.toHaveBeenCalled()
    expect(timers.setTimeout).toHaveBeenCalledOnce()
  })
})
