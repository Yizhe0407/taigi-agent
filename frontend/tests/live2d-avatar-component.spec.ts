import { mount } from "@vue/test-utils"
import { nextTick } from "vue"
import { beforeEach, describe, expect, it, vi } from "vitest"

const avatarHarness = vi.hoisted(() => {
  class Avatar {
    static instances: Avatar[] = []
    readonly load = vi.fn<(signal: AbortSignal) => Promise<void>>()
    readonly dispose = vi.fn<() => void>()
    readonly resize = vi.fn()
    readonly setMouthAmplitude = vi.fn()
    readonly fatal: (error: unknown) => never
    loadSignal: AbortSignal | null = null

    constructor(
      _host: HTMLElement,
      _modelSrc: string,
      fatal: (error: unknown) => never,
    ) {
      this.fatal = fatal
      Avatar.instances.push(this)
      this.load.mockImplementation(
        (signal) =>
          new Promise<void>((resolve, reject) => {
            this.loadSignal = signal
            ;(this as unknown as { resolveLoad: () => void }).resolveLoad = resolve
            ;(this as unknown as { rejectLoad: (error: unknown) => void }).rejectLoad = reject
            signal.addEventListener(
              "abort",
              () => reject(new DOMException("aborted", "AbortError")),
              { once: true },
            )
          }),
      )
    }
  }

  return { Avatar }
})

vi.mock(
  "@/features/agent-chat/live2d/officialCubismAvatar",
  () => ({ OfficialCubismAvatar: avatarHarness.Avatar }),
)

import Live2DAvatar from "@/features/agent-chat/components/Live2DAvatar.vue"

class ResizeObserverMock {
  static instances: ResizeObserverMock[] = []
  static observeError: unknown = null
  readonly observe = vi.fn(() => {
    if (ResizeObserverMock.observeError) throw ResizeObserverMock.observeError
  })
  readonly disconnect = vi.fn()
  readonly callback: ResizeObserverCallback

  constructor(callback: ResizeObserverCallback) {
    this.callback = callback
    ResizeObserverMock.instances.push(this)
  }
}

const flush = async () => {
  await Promise.resolve()
  await nextTick()
  await Promise.resolve()
}

const mountAvatar = (errors: unknown[] = []) =>
  mount(Live2DAvatar, {
    props: {
      modelSrc: "/avatar/model3.json",
      fallbackSrc: "/fallback.png",
      mouthAmplitude: 0,
    },
    global: {
      config: {
        errorHandler: (error) => errors.push(error),
      },
    },
  })

beforeEach(() => {
  avatarHarness.Avatar.instances = []
  ResizeObserverMock.instances = []
  ResizeObserverMock.observeError = null
  vi.stubGlobal("ResizeObserver", ResizeObserverMock)
})

describe("Live2DAvatar component lifecycle", () => {
  it("does not duplicate avatar teardown when an unmounted load rejects late", async () => {
    const wrapper = mountAvatar()
    await nextTick()
    const current = avatarHarness.Avatar.instances[0]!

    wrapper.unmount()
    await flush()

    expect(current.loadSignal?.aborted).toBe(true)
    expect(current.dispose).toHaveBeenCalledOnce()
  })

  it("retains a failed unmount owner without letting the late load continuation retry it", async () => {
    const errors: unknown[] = []
    const wrapper = mountAvatar(errors)
    await nextTick()
    const current = avatarHarness.Avatar.instances[0]!
    const settleTeardown = (wrapper.vm as unknown as {
      settleLive2DTeardown: () => Promise<void>
    }).settleLive2DTeardown
    const cleanupError = new Error("avatar cleanup failed")
    current.dispose.mockImplementationOnce(() => {
      throw cleanupError
    })

    wrapper.unmount()
    await flush()

    expect(current.dispose).toHaveBeenCalledOnce()
    await vi.waitFor(() => expect(errors).toEqual([cleanupError]))

    await expect(settleTeardown()).resolves.toBeUndefined()
    expect(current.dispose).toHaveBeenCalledTimes(2)
  })

  it("claims a ResizeObserver before observe and rolls it back with the avatar", async () => {
    const errors: unknown[] = []
    const wrapper = mountAvatar(errors)
    await nextTick()
    const current = avatarHarness.Avatar.instances[0]!
    const observeError = new Error("observe failed")
    ResizeObserverMock.observeError = observeError
    const resolveLoad = (
      current as unknown as { resolveLoad: () => void }
    ).resolveLoad
    resolveLoad()
    await flush()

    const observer = ResizeObserverMock.instances[0]!

    expect(observer.disconnect).toHaveBeenCalledOnce()
    expect(current.dispose).toHaveBeenCalledOnce()
    await vi.waitFor(() => expect(errors).toEqual([observeError]))
    wrapper.unmount()
  })

  it("reports load and rollback failures while retaining the exact owner for retry", async () => {
    const errors: unknown[] = []
    const wrapper = mountAvatar(errors)
    await nextTick()
    const current = avatarHarness.Avatar.instances[0]!
    const loadError = new Error("avatar load failed")
    const cleanupError = new Error("avatar cleanup failed")
    current.dispose.mockImplementationOnce(() => {
      throw cleanupError
    })

    const rejectLoad = (
      current as unknown as { rejectLoad: (error: unknown) => void }
    ).rejectLoad
    rejectLoad(loadError)
    await flush()

    expect(current.dispose).toHaveBeenCalledOnce()
    await vi.waitFor(() => expect(errors).toHaveLength(1))
    expect(errors[0]).toBeInstanceOf(AggregateError)
    expect((errors[0] as AggregateError).errors).toEqual([
      loadError,
      cleanupError,
    ])

    wrapper.unmount()
    expect(current.dispose).toHaveBeenCalledTimes(2)
  })
})
