/**
 * Repeated-cycle leak regression.
 *
 * Ownership counters prove bookkeeping; WeakRef proves the objects were really
 * released. Requires `--expose-gc` (set by `pnpm test`); without it the suite
 * skips rather than reporting a false pass.
 */
import { mount } from "@vue/test-utils"
import type { Map as MapLibreMap, MarkerOptions } from "maplibre-gl"
import { nextTick, ref, shallowRef } from "vue"
import { describe, expect, it, vi } from "vitest"

import MapMarker from "@/components/ui/map/MapMarker.vue"
import { MapContextKey } from "@/components/ui/map/context"
import { usePipMessageStore } from "@/features/agent-chat/composables/usePipMessageStore"
import { createAsyncReleaseOwner } from "@/lib/async-release-owner"
import { createResourceOwner } from "@/lib/resource-owner"
import { createOwnedInterval, createOwnedTimeout } from "@/lib/timer-owner"

const CYCLES = 25

const collectGarbage = globalThis.gc as (() => void) | undefined

// The fake map retains what is added to it, exactly like MapLibre does: a
// marker that is never removed stays reachable through the map it joined.
type FakeMap = MapLibreMap & { readonly markers: Set<object> }

const markerHarness = vi.hoisted(() => ({ refs: [] as WeakRef<object>[] }))

vi.mock("maplibre-gl", () => {
  type Handler = (...args: unknown[]) => void

  class Marker {
    readonly element = document.createElement("div")
    readonly handlers = new Map<string, Handler>()
    readonly payload = new Uint8Array(4096)
    lng = 120.5
    lat = 23.7

    private map: { markers: Set<object> } | null = null

    constructor(readonly options: Partial<MarkerOptions>) {
      markerHarness.refs.push(new WeakRef(this))
    }

    setLngLat(coordinates: [number, number]) {
      ;[this.lng, this.lat] = coordinates
      return this
    }

    getElement() { return this.element }
    getLngLat() { return { lng: this.lng, lat: this.lat } }
    setDraggable() { return this }
    setOffset() { return this }
    setRotation() { return this }
    setRotationAlignment() { return this }
    setPitchAlignment() { return this }
    on(event: string, handler: Handler) { this.handlers.set(event, handler); return this }
    off(event: string, handler: Handler) {
      if (this.handlers.get(event) === handler) this.handlers.delete(event)
      return this
    }

    addTo(map: { markers: Set<object> }) {
      this.map = map
      map.markers.add(this)
      return this
    }

    remove() {
      this.map?.markers.delete(this)
      this.map = null
      return this
    }
  }

  return { default: { Marker } }
})

async function survivors(refs: WeakRef<object>[]): Promise<object[]> {
  // A WeakRef target stays reachable for the rest of the job that created it,
  // so a real macrotask boundary is required before collection can clear it.
  // Callers must therefore not be holding fake timers here.
  await new Promise(resolve => setTimeout(resolve, 0))
  collectGarbage?.()
  await new Promise(resolve => setTimeout(resolve, 0))
  collectGarbage?.()
  return refs.flatMap((ref) => {
    const alive = ref.deref()
    return alive ? [alive] : []
  })
}

describe("leak cycles", () => {
  // Fail loudly rather than skipping silently: a misconfigured run would
  // otherwise report green with no leak coverage at all.
  it("runs with garbage collection exposed", () => {
    expect(
      typeof collectGarbage,
      "leak cycles need --expose-gc; run `pnpm test`",
    ).toBe("function")
  })

  it.skipIf(!collectGarbage)("releases every claimed resource across owner cycles", async () => {
    // Every cycle runs in its own call frame: a loop body keeps its last
    // iteration's locals reachable and would report a false survivor.
    const cycle = (index: number): WeakRef<object> => {
      const owner = createResourceOwner(`cycle ${index}`)
      const captured = { payload: new Uint8Array(4096) }
      const acquisition = owner.acquire(
        () => ({ handle: captured }),
        () => undefined,
        "captured handle",
      )
      expect(acquisition.active).toBe(true)
      owner.dispose()
      expect(owner.settled).toBe(true)
      return new WeakRef(captured)
    }

    const refs = Array.from({ length: CYCLES }, (_unused, index) => cycle(index))
    expect(await survivors(refs)).toEqual([])
  })

  it.skipIf(!collectGarbage)("releases every timer callback across re-armed interval cycles", async () => {
    const cycle = (index: number): WeakRef<object> => {
      const owner = createResourceOwner(`timers ${index}`)
      const captured = { payload: new Uint8Array(4096) }
      createOwnedTimeout(owner, "one shot", () => void captured, 50)
      const interval = createOwnedInterval(owner, "repeat", () => void captured, 10)
      vi.advanceTimersByTime(35)
      interval.cancel()
      owner.dispose()
      expect(owner.settled).toBe(true)
      return new WeakRef(captured)
    }

    vi.useFakeTimers()
    let refs: WeakRef<object>[]
    try {
      refs = Array.from({ length: CYCLES }, (_unused, index) => cycle(index))
      vi.runAllTimers()
    } finally {
      vi.useRealTimers()
    }

    expect(await survivors(refs)).toEqual([])
  })

  it.skipIf(!collectGarbage)("releases every settled asynchronous release claim", async () => {
    const owner = createAsyncReleaseOwner("async cycles")
    const cycle = async (index: number): Promise<WeakRef<object>> => {
      const captured = { payload: new Uint8Array(4096) }
      const action = owner.claim(`resource ${index}`, async () => void captured)
      await owner.start(action, {})
      return new WeakRef(captured)
    }

    const refs: WeakRef<object>[] = []
    for (let index = 0; index < CYCLES; index += 1) refs.push(await cycle(index))

    expect(owner.settled).toBe(true)
    expect(await survivors(refs)).toEqual([])
  })

  it.skipIf(!collectGarbage)("drops messages past the bound instead of growing history", async () => {
    const { messages, writer } = usePipMessageStore(10)
    const append = (index: number): WeakRef<object> => {
      const payload = { id: `m-${index}`, role: "agent" as const, text: "x".repeat(1024) }
      writer.append(payload)
      return new WeakRef(payload)
    }

    const dropped = Array.from({ length: CYCLES * 4 }, (_unused, index) => append(index))
      .slice(0, CYCLES * 4 - 10)

    expect(messages.value).toHaveLength(10)
    expect(await survivors(dropped)).toEqual([])
  })

  it.skipIf(!collectGarbage)("releases every marker across mount and unmount cycles", async () => {
    const host = { markers: new Set<object>() } as unknown as FakeMap
    const map = shallowRef<MapLibreMap | null>(host)
    const cycle = async (): Promise<void> => {
      const wrapper = mount(MapMarker, {
        props: { longitude: 120.5, latitude: 23.7 },
        global: { provide: { [MapContextKey as symbol]: { map, isLoaded: ref(true) } } },
      })
      await nextTick()
      wrapper.unmount()
      await nextTick()
    }

    markerHarness.refs = []
    for (let index = 0; index < CYCLES; index += 1) await cycle()

    expect(markerHarness.refs).toHaveLength(CYCLES)
    expect(host.markers.size).toBe(0)
    expect(await survivors(markerHarness.refs)).toEqual([])
  })
})
