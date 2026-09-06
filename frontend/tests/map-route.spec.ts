import { mount } from "@vue/test-utils"
import type { Map as MapLibreMap } from "maplibre-gl"
import { nextTick, ref, shallowRef } from "vue"
import { describe, expect, it, vi } from "vitest"

import MapRoute from "@/components/ui/map/MapRoute.vue"
import { MapContextKey } from "@/components/ui/map/context"

const coordinates: [number, number][] = [
  [120.5, 23.7],
  [120.6, 23.8],
]

describe("MapRoute", () => {
  it("removes every acquired resource using the interactive setup snapshot", async () => {
    const on = vi.fn()
    const off = vi.fn()
    const map = {
      addSource: vi.fn(),
      addLayer: vi.fn(),
      getLayer: vi.fn(() => ({})),
      getSource: vi.fn(() => ({})),
      removeLayer: vi.fn(),
      removeSource: vi.fn(),
      on,
      off,
    } as unknown as MapLibreMap

    const wrapper = mount(MapRoute, {
      props: { coordinates, interactive: true },
      global: {
        provide: {
          [MapContextKey as symbol]: {
            map: shallowRef(map),
            isLoaded: ref(true),
          },
        },
      },
    })

    await nextTick()
    expect(on).toHaveBeenCalledOnce()
    const registeredHandler = on.mock.calls[0]?.[2]
    const layerId = on.mock.calls[0]?.[1]
    const sourceId = vi.mocked(map.addSource).mock.calls[0]?.[0]

    await wrapper.setProps({ interactive: false })
    wrapper.unmount()
    registeredHandler?.()

    expect(wrapper.emitted("click")).toBeUndefined()
    expect(off).toHaveBeenCalledOnce()
    expect(off).toHaveBeenCalledWith("click", layerId, registeredHandler)
    expect(map.removeLayer).toHaveBeenCalledOnce()
    expect(map.removeLayer).toHaveBeenCalledWith(layerId)
    expect(map.removeSource).toHaveBeenCalledOnce()
    expect(map.removeSource).toHaveBeenCalledWith(sourceId)
  })

  it("rolls back source and layer acquisition when listener setup throws", () => {
    const setupError = new Error("listener setup failed")
    const map = {
      addSource: vi.fn(),
      addLayer: vi.fn(),
      getLayer: vi.fn(() => ({})),
      getSource: vi.fn(() => ({})),
      removeLayer: vi.fn(),
      removeSource: vi.fn(),
      on: vi.fn(() => {
        throw setupError
      }),
      off: vi.fn(),
    } as unknown as MapLibreMap

    expect(() => mount(MapRoute, {
      props: { id: "rollback", coordinates, interactive: true },
      global: {
        provide: {
          [MapContextKey as symbol]: {
            map: shallowRef(map),
            isLoaded: ref(true),
          },
        },
      },
    })).toThrow(setupError)

    expect(map.removeLayer).toHaveBeenCalledOnce()
    expect(map.removeLayer).toHaveBeenCalledWith("route-layer-rollback")
    expect(map.removeSource).toHaveBeenCalledOnce()
    expect(map.removeSource).toHaveBeenCalledWith("route-source-rollback")
    expect(map.off).toHaveBeenCalledOnce()
    expect(map.off).toHaveBeenCalledWith(
      "click",
      "route-layer-rollback",
      expect.any(Function),
    )
  })
})
