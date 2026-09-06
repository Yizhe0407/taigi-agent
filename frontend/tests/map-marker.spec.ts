import { mount } from "@vue/test-utils"
import type { Map as MapLibreMap, MarkerOptions } from "maplibre-gl"
import { createApp, defineComponent, h, nextTick, provide, ref, shallowRef } from "vue"
import { beforeEach, describe, expect, it, vi } from "vitest"

import MapMarker from "@/components/ui/map/MapMarker.vue"
import { MapContextKey } from "@/components/ui/map/context"
import { createApplicationLifecycle } from "@/lib/application-lifecycle"

const markerHarness = vi.hoisted(() => {
  type Handler = (...args: unknown[]) => void

  class Marker {
    static instances: Marker[] = []
    static addToError: Error | null = null
    static onError: Error | null = null
    static offError: Error | null = null
    static removeError: Error | null = null
    static setterError: Error | null = null

    readonly element = document.createElement("div")
    readonly handlers = new Map<string, Handler>()
    readonly options: Partial<MarkerOptions>
    lng = 120.5
    lat = 23.7

    readonly setLngLat = vi.fn((coordinates: [number, number]) => {
      if (Marker.setterError) throw Marker.setterError
      ;[this.lng, this.lat] = coordinates
      return this
    })
    readonly getElement = vi.fn(() => this.element)
    readonly getLngLat = vi.fn(() => ({ lng: this.lng, lat: this.lat }))
    readonly setDraggable = vi.fn(() => this)
    readonly setOffset = vi.fn(() => this)
    readonly setRotation = vi.fn(() => this)
    readonly setRotationAlignment = vi.fn(() => this)
    readonly setPitchAlignment = vi.fn(() => this)
    readonly on = vi.fn((event: string, handler: Handler) => {
      this.handlers.set(event, handler)
      if (Marker.onError) throw Marker.onError
      return this
    })
    readonly off = vi.fn((event: string, handler: Handler) => {
      if (this.handlers.get(event) === handler) this.handlers.delete(event)
      if (Marker.offError && this.off.mock.calls.length === 1) {
        throw Marker.offError
      }
      return this
    })
    readonly addTo = vi.fn((_map: MapLibreMap) => {
      if (Marker.addToError) throw Marker.addToError
      return this
    })
    readonly remove = vi.fn(() => {
      if (Marker.removeError && this.remove.mock.calls.length === 1) {
        throw Marker.removeError
      }
      return this
    })

    constructor(options: Partial<MarkerOptions>) {
      this.options = options
      Marker.instances.push(this)
    }
  }

  return { Marker }
})

vi.mock("maplibre-gl", () => ({
  default: { Marker: markerHarness.Marker },
}))

function mountMarker(
  map: ReturnType<typeof shallowRef<MapLibreMap | null>>,
  errorHandler?: (error: unknown) => void,
) {
  return mount(MapMarker, {
    props: { longitude: 120.5, latitude: 23.7 },
    global: {
      provide: {
        [MapContextKey as symbol]: { map, isLoaded: ref(true) },
      },
      config: { errorHandler },
    },
  })
}

describe("MapMarker", () => {
  beforeEach(() => {
    markerHarness.Marker.instances = []
    markerHarness.Marker.addToError = null
    markerHarness.Marker.onError = null
    markerHarness.Marker.offError = null
    markerHarness.Marker.removeError = null
    markerHarness.Marker.setterError = null
  })

  it("moves one marker owner without duplicate configuration or teardown", async () => {
    const firstMap = {} as MapLibreMap
    const secondMap = {} as MapLibreMap
    const map = shallowRef<MapLibreMap | null>(firstMap)
    const wrapper = mountMarker(map)
    await nextTick()

    const marker = markerHarness.Marker.instances[0]!
    const removeElementListener = vi.spyOn(marker.element, "removeEventListener")
    expect(marker.options.element).toBeInstanceOf(HTMLElement)
    expect(marker.setLngLat).toHaveBeenCalledOnce()
    expect(marker.setDraggable).toHaveBeenCalledOnce()
    expect(marker.setOffset).toHaveBeenCalledOnce()
    expect(marker.setRotation).toHaveBeenCalledOnce()
    expect(marker.setRotationAlignment).toHaveBeenCalledOnce()
    expect(marker.setPitchAlignment).toHaveBeenCalledOnce()
    expect(marker.addTo).toHaveBeenCalledOnce()
    expect(marker.addTo).toHaveBeenCalledWith(firstMap)

    marker.element.dispatchEvent(new MouseEvent("click"))
    marker.handlers.get("drag")?.()
    const clickEvents = wrapper.emitted("click")!
    const dragEvents = wrapper.emitted("drag")!
    expect(clickEvents).toHaveLength(1)
    expect(dragEvents).toEqual([[{ lng: 120.5, lat: 23.7 }]])

    map.value = secondMap
    await nextTick()
    expect(marker.remove).toHaveBeenCalledOnce()
    expect(marker.addTo).toHaveBeenLastCalledWith(secondMap)

    map.value = null
    await nextTick()
    expect(marker.remove).toHaveBeenCalledTimes(2)
    expect(marker.addTo).toHaveBeenCalledTimes(2)

    const dragHandler = marker.handlers.get("drag")
    wrapper.unmount()

    expect(removeElementListener).toHaveBeenCalledWith("click", expect.any(Function))
    expect(marker.off).toHaveBeenCalledWith("drag", dragHandler)
    // It was already detached when the map became null; unmount must not issue
    // a redundant third remove().
    expect(marker.remove).toHaveBeenCalledTimes(2)
    expect(marker.handlers.has("drag")).toBe(false)

    marker.element.dispatchEvent(new MouseEvent("click"))
    dragHandler?.()
    expect(clickEvents).toHaveLength(1)
    expect(dragEvents).toHaveLength(1)
  })

  it("rolls back conservatively when listener registration mutates then throws", () => {
    const setupError = new Error("drag listener failed")
    markerHarness.Marker.onError = setupError

    expect(() => mountMarker(shallowRef({} as MapLibreMap))).toThrow(setupError)

    const marker = markerHarness.Marker.instances[0]!
    expect(marker.off).toHaveBeenCalledWith("drag", expect.any(Function))
    expect(marker.handlers.has("drag")).toBe(false)
    expect(marker.addTo).not.toHaveBeenCalled()
    expect(marker.remove).not.toHaveBeenCalled()
  })

  it("defers a failed listener rollback until the next application teardown attempt", async () => {
    const setupError = new Error("drag listener failed after registration")
    const cleanupError = new Error("drag listener removal failed")
    markerHarness.Marker.onError = setupError
    markerHarness.Marker.offError = cleanupError
    const map = shallowRef<MapLibreMap | null>({} as MapLibreMap)
    const Root = defineComponent({
      setup() {
        provide(MapContextKey, { map, isLoaded: ref(true) })
        return () => h(MapMarker, { longitude: 120.5, latitude: 23.7 })
      },
    })
    const target = document.createElement("div")
    document.body.append(target)
    const app = createApp(Root)
    const handledErrors: unknown[] = []
    app.config.errorHandler = error => handledErrors.push(error)
    const lifecycle = createApplicationLifecycle({
      app,
      mountTarget: target,
      reportClientEvent: vi.fn(),
      shutdownClientEventReporting: vi.fn(async () => {}),
    })

    await lifecycle.start()
    const marker = markerHarness.Marker.instances[0]!
    expect(handledErrors).toHaveLength(1)
    expect(handledErrors[0]).toMatchObject({
      message: "Failed to acquire and roll back MapLibre marker drag listener",
    })
    expect(marker.off).toHaveBeenCalledOnce()
    expect(marker.remove).not.toHaveBeenCalled()

    await lifecycle.teardown()
    expect(marker.off).toHaveBeenCalledTimes(2)
    expect(marker.remove).not.toHaveBeenCalled()
    target.remove()
  })

  it("terminally releases the marker if initial map attachment fails", () => {
    const addError = new Error("marker add failed")
    markerHarness.Marker.addToError = addError

    expect(() => mountMarker(shallowRef({} as MapLibreMap))).toThrow(addError)

    const marker = markerHarness.Marker.instances[0]!
    const dragHandler = marker.off.mock.calls[0]?.[1]
    expect(marker.off).toHaveBeenCalledWith("drag", dragHandler)
    expect(marker.handlers.has("drag")).toBe(false)
    expect(marker.remove).toHaveBeenCalledOnce()
  })

  it("does not revive a marker after replacement attachment fails", async () => {
    const map = shallowRef<MapLibreMap | null>({} as MapLibreMap)
    const wrapper = mountMarker(map)
    await nextTick()
    const marker = markerHarness.Marker.instances[0]!
    const cachedDrag = marker.handlers.get("drag")

    markerHarness.Marker.addToError = new Error("replacement failed")
    map.value = {} as MapLibreMap
    await expect(nextTick()).rejects.toThrow("replacement failed")

    const addCallsAfterFailure = marker.addTo.mock.calls.length
    map.value = {} as MapLibreMap
    await nextTick()
    cachedDrag?.()

    expect(marker.addTo).toHaveBeenCalledTimes(addCallsAfterFailure)
    expect(wrapper.emitted("drag")).toBeUndefined()
    wrapper.unmount()
  })

  it("does not immediately retry a failed attachment release during map replacement", async () => {
    const cleanupError = new Error("replacement detach failed")
    markerHarness.Marker.removeError = cleanupError
    const map = shallowRef<MapLibreMap | null>({} as MapLibreMap)
    const wrapper = mountMarker(map)
    await nextTick()
    const marker = markerHarness.Marker.instances[0]!
    const settleMarkerTeardown = (wrapper.vm as unknown as {
      settleMarkerTeardown: () => void
    }).settleMarkerTeardown

    map.value = {} as MapLibreMap
    await expect(nextTick()).rejects.toThrow(cleanupError)

    expect(marker.remove).toHaveBeenCalledOnce()
    expect(marker.addTo).toHaveBeenCalledOnce()
    expect(() => settleMarkerTeardown()).not.toThrow()
    expect(marker.remove).toHaveBeenCalledTimes(2)
    expect(marker.addTo).toHaveBeenCalledOnce()
    wrapper.unmount()
  })

  it("terminally tears down if a reactive setter fails", async () => {
    const map = shallowRef<MapLibreMap | null>({} as MapLibreMap)
    const wrapper = mountMarker(map)
    await nextTick()
    const marker = markerHarness.Marker.instances[0]!
    const cachedDrag = marker.handlers.get("drag")

    markerHarness.Marker.setterError = new Error("setter failed")
    await expect(wrapper.setProps({ longitude: 120.6 })).rejects.toThrow("setter failed")

    expect(marker.off).toHaveBeenCalledWith("drag", cachedDrag)
    expect(marker.remove).toHaveBeenCalledOnce()
    cachedDrag?.()
    expect(wrapper.emitted("drag")).toBeUndefined()
    wrapper.unmount()
  })

  it("retains a failed marker attachment and retries the same remove claim", async () => {
    const cleanupError = new Error("marker remove failed after detaching")
    markerHarness.Marker.removeError = cleanupError
    const handledErrors: unknown[] = []
    const map = shallowRef<MapLibreMap | null>({} as MapLibreMap)
    const wrapper = mountMarker(map, error => handledErrors.push(error))
    await nextTick()
    const marker = markerHarness.Marker.instances[0]!
    const cachedDrag = marker.handlers.get("drag")
    const settleMarkerTeardown = (wrapper.vm as unknown as {
      settleMarkerTeardown: () => void
    }).settleMarkerTeardown

    wrapper.unmount()

    expect(handledErrors).toEqual([cleanupError])
    expect(marker.remove).toHaveBeenCalledOnce()
    expect(marker.addTo).toHaveBeenCalledOnce()
    expect(marker.handlers.has("drag")).toBe(false)

    expect(() => settleMarkerTeardown()).not.toThrow()
    expect(marker.remove).toHaveBeenCalledTimes(2)
    expect(marker.addTo).toHaveBeenCalledOnce()

    cachedDrag?.()
    marker.element.dispatchEvent(new MouseEvent("click"))
    expect(wrapper.emitted("drag")).toBeUndefined()
    expect(wrapper.emitted("click")).toBeUndefined()
  })
})
