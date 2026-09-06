import { mount } from "@vue/test-utils"
import type { Map as MapLibreMap } from "maplibre-gl"
import {
  defineComponent,
  effectScope,
  nextTick,
  ref,
  shallowRef,
} from "vue"
import { describe, expect, it, vi } from "vitest"

import { useMapLayer } from "@/components/ui/map/composables/use-map-layer"
import type { MapResourceOwner } from "@/components/ui/map/composables/use-map-layer"

describe("useMapLayer", () => {
  it("tears down the old map before setting up a replacement and on unmount", async () => {
    const firstMap = {} as MapLibreMap
    const secondMap = {} as MapLibreMap
    const map = shallowRef<MapLibreMap | null>(firstMap)
    const loaded = ref(true)
    const firstCleanup = vi.fn()
    const secondCleanup = vi.fn()
    const setup = vi.fn(
      (
        mapInstance: MapLibreMap,
        owner: MapResourceOwner,
      ) => {
        owner.acquire(
          () => mapInstance,
          mapInstance === firstMap ? firstCleanup : secondCleanup,
        )
      },
    )

    const wrapper = mount(
      defineComponent({
        setup() {
          useMapLayer(map, loaded, "test layer", setup)
          return () => null
        },
      }),
    )

    expect(setup).toHaveBeenCalledWith(
      firstMap,
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
      expect.any(Function),
    )

    map.value = secondMap
    await nextTick()

    expect(firstCleanup).toHaveBeenCalledOnce()
    expect(setup).toHaveBeenLastCalledWith(
      secondMap,
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
      expect.any(Function),
    )

    wrapper.unmount()
    expect(secondCleanup).toHaveBeenCalledOnce()

    map.value = firstMap
    loaded.value = false
    await nextTick()
    expect(setup).toHaveBeenCalledTimes(2)
  })

  it("cleans other claims but defers failed setup rollback debt to scope teardown", () => {
    const map = shallowRef<MapLibreMap | null>({} as MapLibreMap)
    const loaded = ref(true)
    const setupError = new Error("listener registration failed")
    const cleanupError = new Error("listener removal failed")
    const otherRelease = vi.fn()
    const release = vi
      .fn<() => void>()
      .mockImplementationOnce(() => {
        throw cleanupError
      })
    const scope = effectScope()

    expect(() =>
      scope.run(() =>
        useMapLayer(map, loaded, "retryable layer", (_map, owner) => {
          owner.acquire(() => undefined, otherRelease)
          owner.acquire(() => {
            throw setupError
          }, release)
        }),
      ),
    ).toThrowError(AggregateError)

    expect(release).toHaveBeenCalledOnce()
    expect(otherRelease).toHaveBeenCalledOnce()
    expect(() => scope.stop()).not.toThrow()
    expect(release).toHaveBeenCalledTimes(2)
    expect(otherRelease).toHaveBeenCalledOnce()
  })

  it("terminally gates callbacks before releasing a failed active layer", () => {
    const map = shallowRef<MapLibreMap | null>({} as MapLibreMap)
    const loaded = ref(true)
    const release = vi.fn()
    let cachedCallback = () => {}
    let failLayer: ((error: unknown) => never) | null = null

    const wrapper = mount(
      defineComponent({
        setup() {
          useMapLayer(
            map,
            loaded,
            "terminal layer",
            (_map, owner, fail) => {
              failLayer = fail
              cachedCallback = () => {
                if (!owner.signal.aborted) release()
              }
              owner.acquire(() => undefined, release)
            },
          )
          return () => null
        },
      }),
    )

    cachedCallback()
    expect(release).toHaveBeenCalledOnce()
    expect(() => failLayer?.(new Error("reactive mutation failed"))).toThrow(
      "reactive mutation failed",
    )
    cachedCallback()
    expect(release).toHaveBeenCalledTimes(2)

    wrapper.unmount()
    expect(release).toHaveBeenCalledTimes(2)
  })
  it("does not let a predecessor failure dispose the successor generation", async () => {
    const firstMap = {} as MapLibreMap
    const secondMap = {} as MapLibreMap
    const map = shallowRef<MapLibreMap | null>(firstMap)
    const loaded = ref(true)
    const firstRelease = vi.fn()
    const secondRelease = vi.fn()
    let predecessorFail: ((error: unknown) => never) | null = null

    const wrapper = mount(
      defineComponent({
        setup() {
          useMapLayer(
            map,
            loaded,
            "generation-fenced layer",
            (mapInstance, owner, fail) => {
              owner.acquire(
                () => undefined,
                mapInstance === firstMap ? firstRelease : secondRelease,
              )
              if (mapInstance === firstMap) predecessorFail = fail
            },
          )
          return () => null
        },
      }),
    )

    map.value = secondMap
    await nextTick()
    expect(firstRelease).toHaveBeenCalledOnce()
    expect(secondRelease).not.toHaveBeenCalled()

    const lateError = new Error("late predecessor failure")
    expect(() => predecessorFail?.(lateError)).toThrow(lateError)
    expect(secondRelease).not.toHaveBeenCalled()

    wrapper.unmount()
    expect(secondRelease).toHaveBeenCalledOnce()
  })

})
