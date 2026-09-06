import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

const live2dHarness = vi.hoisted(() => {
  const setting = {
    textureCount: 0,
  }

  class ModelSetting {
    getModelFileName() {
      return "avatar.moc3"
    }

    getLayoutMap() {
      return true
    }

    getTextureDirectory() {
      return "textures"
    }

    getTextureCount() {
      return setting.textureCount
    }

    getTextureFileName(index: number) {
      return `texture-${index}.png`
    }
  }

  class Matrix {
    scale() {}
    multiplyByMatrix() {}
  }

  const frameworkState = {
    started: true,
    initialized: true,
  }
  const framework = {
    isStarted: vi.fn(() => frameworkState.started),
    isInitialized: vi.fn(() => frameworkState.initialized),
    startUp: vi.fn(() => {
      frameworkState.started = true
      return true
    }),
    initialize: vi.fn(() => {
      frameworkState.initialized = true
    }),
    dispose: vi.fn(() => {
      frameworkState.initialized = false
    }),
    cleanUp: vi.fn(() => {
      frameworkState.started = false
    }),
    getIdManager: vi.fn(() => ({ getId: (name: string) => name })),
  }

  const userModels: UserModel[] = []
  class UserModel {
    readonly renderer = {
      startUp: vi.fn(),
      setIsPremultipliedAlpha: vi.fn(),
      bindTexture: vi.fn(),
    }
    readonly modelMatrix = { setupFromLayout: vi.fn() }
    readonly loadModel = vi.fn()
    readonly getModelMatrix = vi.fn(() => this.modelMatrix)
    readonly createRenderer = vi.fn()
    readonly getRenderer = vi.fn(() => this.renderer)
    readonly release = vi.fn()

    constructor() {
      userModels.push(this)
    }
  }

  return {
    setting,
    frameworkState,
    framework,
    ModelSetting,
    Matrix,
    UserModel,
    userModels,
  }
})

vi.mock("@/vendor/live2d/framework/live2dcubismframework.js", () => ({
  CubismFramework: live2dHarness.framework,
  Option: class {},
  LogLevel: { LogLevel_Off: 0 },
}))

vi.mock("@/vendor/live2d/framework/cubismmodelsettingjson.js", () => ({
  CubismModelSettingJson: live2dHarness.ModelSetting,
}))

vi.mock("@/vendor/live2d/framework/math/cubismmatrix44.js", () => ({
  CubismMatrix44: live2dHarness.Matrix,
}))

vi.mock("@/vendor/live2d/framework/model/cubismusermodel.js", () => ({
  CubismUserModel: live2dHarness.UserModel,
}))

const importAvatar = async () => {
  const module = await import(
    "@/features/agent-chat/live2d/officialCubismAvatar"
  )
  return module.OfficialCubismAvatar
}

const createHost = () => {
  const host = document.createElement("div")
  Object.defineProperty(host, "getBoundingClientRect", {
    configurable: true,
    value: () => ({ width: 320, height: 480 }),
  })
  document.body.appendChild(host)
  return host
}

beforeEach(() => {
  vi.resetModules()
  live2dHarness.setting.textureCount = 0
  live2dHarness.frameworkState.started = true
  live2dHarness.frameworkState.initialized = true
  live2dHarness.framework.isStarted.mockReset().mockImplementation(
    () => live2dHarness.frameworkState.started,
  )
  live2dHarness.framework.isInitialized.mockReset().mockImplementation(
    () => live2dHarness.frameworkState.initialized,
  )
  live2dHarness.framework.startUp.mockReset().mockImplementation(() => {
    live2dHarness.frameworkState.started = true
    return true
  })
  live2dHarness.framework.initialize.mockReset().mockImplementation(() => {
    live2dHarness.frameworkState.initialized = true
  })
  live2dHarness.framework.dispose.mockReset().mockImplementation(() => {
    live2dHarness.frameworkState.initialized = false
  })
  live2dHarness.framework.cleanUp.mockReset().mockImplementation(() => {
    live2dHarness.frameworkState.started = false
  })
  live2dHarness.framework.getIdManager.mockClear()
  live2dHarness.userModels.length = 0
  Object.defineProperty(window, "Live2DCubismCore", {
    configurable: true,
    writable: true,
    value: {},
  })
})

afterEach(() => {
  document.body.replaceChildren()
  document.head.querySelectorAll('script[src="/vendor/live2dcubismcore.min.js"]').forEach((node) => node.remove())
  vi.restoreAllMocks()
})

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}

describe("OfficialCubismAvatar ownership", () => {
  it("permanently gates an unknowable frame when scheduling mutates then throws", async () => {
    const setupError = new Error("animation frame scheduled then failed")
    let cachedCallback: FrameRequestCallback | null = null
    const requestAnimationFrame = vi.spyOn(globalThis, "requestAnimationFrame")
      .mockImplementation((callback) => {
        cachedCallback = callback
        throw setupError
      })
    const cancelAnimationFrame = vi.spyOn(globalThis, "cancelAnimationFrame")

    const OfficialCubismAvatar = await importAvatar()
    const avatar = new OfficialCubismAvatar(
      createHost(),
      "/avatar/model3.json",
      (error) => { throw error },
    )
    const render = vi.fn()
    const internals = avatar as unknown as {
      render: () => void
      start: () => void
    }
    internals.render = render

    expect(() => internals.start()).toThrow(setupError)
    expect(render).toHaveBeenCalledOnce()
    expect(requestAnimationFrame).toHaveBeenCalledOnce()
    expect(cancelAnimationFrame).not.toHaveBeenCalled()

    ;(cachedCallback as FrameRequestCallback | null)?.(100)
    expect(render).toHaveBeenCalledOnce()
    expect(requestAnimationFrame).toHaveBeenCalledOnce()
    expect(() => avatar.dispose()).not.toThrow()
  })

  it("retries a failed exact frame cancellation only on a distinct dispose attempt", async () => {
    let cachedCallback: FrameRequestCallback | null = null
    const requestAnimationFrame = vi.spyOn(globalThis, "requestAnimationFrame")
      .mockImplementation((callback) => {
        cachedCallback = callback
        return 73
      })
    const cancelError = new Error("animation frame cancellation failed")
    const cancelAnimationFrame = vi.spyOn(globalThis, "cancelAnimationFrame")
      .mockImplementationOnce(() => {
        throw cancelError
      })

    const OfficialCubismAvatar = await importAvatar()
    const avatar = new OfficialCubismAvatar(
      createHost(),
      "/avatar/model3.json",
      (error) => { throw error },
    )
    const render = vi.fn()
    const internals = avatar as unknown as {
      render: () => void
      start: () => void
    }
    internals.render = render
    internals.start()
    const staleCallback = cachedCallback
    const firstAttempt = {}

    expect(() => avatar.dispose(firstAttempt)).toThrow(cancelError)
    expect(() => avatar.dispose(firstAttempt)).not.toThrow()
    expect(cancelAnimationFrame).toHaveBeenCalledOnce()

    ;(staleCallback as FrameRequestCallback | null)?.(100)
    expect(render).toHaveBeenCalledOnce()
    expect(requestAnimationFrame).toHaveBeenCalledOnce()

    expect(() => avatar.dispose({})).not.toThrow()
    expect(cancelAnimationFrame.mock.calls.map(([id]) => id)).toEqual([73, 73])
  })

  it("retains a mutating lifecycle abort failure for a distinct dispose attempt", async () => {
    const abortError = new Error("lifecycle abort mutated then failed")
    const originalAbort = AbortController.prototype.abort
    const abort = vi.spyOn(AbortController.prototype, "abort")
      .mockImplementationOnce(function (this: AbortController, reason?: unknown) {
        originalAbort.call(this, reason)
        throw abortError
      })
      .mockImplementation(function (this: AbortController, reason?: unknown) {
        originalAbort.call(this, reason)
      })

    const OfficialCubismAvatar = await importAvatar()
    const avatar = new OfficialCubismAvatar(
      createHost(),
      "/avatar/model3.json",
      (error) => { throw error },
    )
    const internals = avatar as unknown as {
      lifetimeOwner: { signal: AbortSignal; settled: boolean }
    }
    const firstAttempt = {}

    expect(() => avatar.dispose(firstAttempt)).toThrow(abortError)
    expect(internals.lifetimeOwner.signal.aborted).toBe(true)
    expect(internals.lifetimeOwner.settled).toBe(false)
    expect(() => avatar.dispose(firstAttempt)).not.toThrow()
    expect(abort).toHaveBeenCalledOnce()

    expect(() => avatar.dispose({})).not.toThrow()
    expect(abort).toHaveBeenCalledTimes(2)
    expect(internals.lifetimeOwner.settled).toBe(true)
  })

  it("does not re-arm after a fatal frame and propagates its exact teardown attempt", async () => {
    let cachedCallback: FrameRequestCallback | null = null
    const requestAnimationFrame = vi.spyOn(globalThis, "requestAnimationFrame")
      .mockImplementation((callback) => {
        cachedCallback = callback
        return 91
      })
    const cancelAnimationFrame = vi.spyOn(globalThis, "cancelAnimationFrame")
    const renderError = new Error("frame render failed")
    let fatalAttempt: object | null = null
    let avatar!: InstanceType<Awaited<ReturnType<typeof importAvatar>>>

    const OfficialCubismAvatar = await importAvatar()
    avatar = new OfficialCubismAvatar(
      createHost(),
      "/avatar/model3.json",
      (error, attempt) => {
        fatalAttempt = attempt
        avatar.dispose(attempt)
        throw error
      },
    )
    const render = vi.fn()
      .mockImplementationOnce(() => undefined)
      .mockImplementationOnce(() => { throw renderError })
    const internals = avatar as unknown as {
      render: () => void
      start: () => void
    }
    internals.render = render
    internals.start()
    const callback = cachedCallback

    expect(() => callback?.(100)).toThrow(renderError)
    expect(fatalAttempt).not.toBeNull()
    expect(render).toHaveBeenCalledTimes(2)
    expect(requestAnimationFrame).toHaveBeenCalledOnce()
    expect(cancelAnimationFrame).not.toHaveBeenCalled()

    callback?.(200)
    expect(render).toHaveBeenCalledTimes(2)
    expect(requestAnimationFrame).toHaveBeenCalledOnce()
  })

  it("rolls back a false framework startup and retries from a fresh framework operation", async () => {
    live2dHarness.frameworkState.started = false
    live2dHarness.frameworkState.initialized = false
    live2dHarness.framework.startUp
      .mockImplementationOnce(() => false)
      .mockImplementationOnce(() => {
        live2dHarness.frameworkState.started = true
        return true
      })
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(null)

    const OfficialCubismAvatar = await importAvatar()
    const first = new OfficialCubismAvatar(createHost(), "/avatar/model3.json", () => {
      throw new Error("fatal")
    })
    await expect(first.load(new AbortController().signal)).rejects.toThrow(
      "Live2D Cubism Framework failed to start",
    )
    expect(live2dHarness.framework.cleanUp).toHaveBeenCalledOnce()
    expect(live2dHarness.framework.initialize).not.toHaveBeenCalled()
    expect(live2dHarness.userModels).toHaveLength(0)
    first.dispose()

    const second = new OfficialCubismAvatar(createHost(), "/avatar/model3.json", () => {
      throw new Error("fatal")
    })
    await expect(second.load(new AbortController().signal)).rejects.toThrow(
      "Unable to create a WebGL context for Live2D",
    )
    expect(live2dHarness.framework.startUp).toHaveBeenCalledTimes(2)
    expect(live2dHarness.framework.initialize).toHaveBeenCalledOnce()
    second.dispose()
  })

  it("cleans up framework startup even when startup mutates before throwing", async () => {
    const startupError = new Error("framework startup mutated then failed")
    live2dHarness.frameworkState.started = false
    live2dHarness.frameworkState.initialized = false
    live2dHarness.framework.startUp.mockImplementation(() => {
      live2dHarness.frameworkState.started = true
      throw startupError
    })

    const OfficialCubismAvatar = await importAvatar()
    const avatar = new OfficialCubismAvatar(createHost(), "/avatar/model3.json", () => {
      throw new Error("fatal")
    })
    await expect(avatar.load(new AbortController().signal)).rejects.toBe(startupError)
    expect(live2dHarness.framework.cleanUp).toHaveBeenCalledOnce()
    expect(live2dHarness.frameworkState.started).toBe(false)
    expect(live2dHarness.framework.initialize).not.toHaveBeenCalled()
    avatar.dispose()
  })

  it("aggregates initialization failure with both framework rollback failures", async () => {
    const primaryError = new Error("framework initialization failed")
    const disposeError = new Error("framework dispose failed")
    const cleanupError = new Error("framework cleanup failed")
    live2dHarness.frameworkState.started = false
    live2dHarness.frameworkState.initialized = false
    live2dHarness.framework.initialize.mockImplementation(() => {
      live2dHarness.frameworkState.initialized = true
      throw primaryError
    })
    live2dHarness.framework.dispose.mockImplementation(() => {
      live2dHarness.frameworkState.initialized = false
      throw disposeError
    })
    live2dHarness.framework.cleanUp.mockImplementation(() => {
      live2dHarness.frameworkState.started = false
      throw cleanupError
    })

    const OfficialCubismAvatar = await importAvatar()
    const avatar = new OfficialCubismAvatar(createHost(), "/avatar/model3.json", () => {
      throw new Error("fatal")
    })
    const failure = await avatar.load(new AbortController().signal).catch(error => error)

    expect(failure).toBeInstanceOf(AggregateError)
    expect((failure as AggregateError).errors).toEqual([
      primaryError,
      disposeError,
      cleanupError,
    ])
    expect(live2dHarness.framework.dispose).toHaveBeenCalledOnce()
    expect(live2dHarness.framework.cleanUp).toHaveBeenCalledOnce()
    avatar.dispose()
  })

  it("removes a Core script whose append mutated before throwing and creates one fresh retry", async () => {
    Object.defineProperty(window, "Live2DCubismCore", {
      configurable: true,
      writable: true,
      value: undefined,
    })
    const appendError = new Error("Core script append failed after mutation")
    const scripts: HTMLScriptElement[] = []
    const removeScript = vi.spyOn(HTMLScriptElement.prototype, "remove")
    let appendCount = 0
    vi.spyOn(document.head, "appendChild").mockImplementation((node) => {
      const script = node as HTMLScriptElement
      scripts.push(script)
      script.dataset.claimedByHead = "true"
      appendCount += 1
      if (appendCount === 1) throw appendError
      return node
    })

    const OfficialCubismAvatar = await importAvatar()
    const first = new OfficialCubismAvatar(createHost(), "/avatar/model3.json", () => {
      throw new Error("fatal")
    })
    await expect(first.load(new AbortController().signal)).rejects.toBe(appendError)
    const firstScript = scripts[0]!
    expect(firstScript.onload).toBeNull()
    expect(firstScript.onerror).toBeNull()
    expect(removeScript.mock.contexts[0]).toBe(firstScript)
    first.dispose()

    const second = new OfficialCubismAvatar(createHost(), "/avatar/model3.json", () => {
      throw new Error("fatal")
    })
    const secondLoad = second.load(new AbortController().signal)
    await vi.waitFor(() => expect(scripts).toHaveLength(2))
    const secondScript = scripts[1]!
    expect(secondScript).not.toBe(firstScript)
    secondScript.onerror?.(new Event("error"))
    await expect(secondLoad).rejects.toThrow("Unable to load Live2D Cubism Core")
    expect(removeScript.mock.contexts[1]).toBe(secondScript)
    second.dispose()
  })

  it("keeps abort authoritative across late arrayBuffer resolution and rejection", async () => {
    const gl = { getExtension: vi.fn(() => null) }
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(
      gl as unknown as RenderingContext,
    )
    const firstBody = deferred<ArrayBuffer>()
    const secondBody = deferred<ArrayBuffer>()
    const arrayBuffer = vi.fn()
      .mockImplementationOnce(() => firstBody.promise)
      .mockImplementationOnce(() => secondBody.promise)
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, arrayBuffer })))

    const OfficialCubismAvatar = await importAvatar()
    const firstController = new AbortController()
    const first = new OfficialCubismAvatar(createHost(), "/avatar/model3.json", () => {
      throw new Error("fatal")
    })
    const firstLoad = first.load(firstController.signal)
    await vi.waitFor(() => expect(arrayBuffer).toHaveBeenCalledOnce())
    const firstAbort = new DOMException("first load cancelled", "AbortError")
    firstController.abort(firstAbort)
    firstBody.resolve(new ArrayBuffer(8))
    await expect(firstLoad).rejects.toBe(firstAbort)
    expect(live2dHarness.userModels).toHaveLength(0)
    first.dispose()

    const secondController = new AbortController()
    const second = new OfficialCubismAvatar(createHost(), "/avatar/model3.json", () => {
      throw new Error("fatal")
    })
    const secondLoad = second.load(secondController.signal)
    await vi.waitFor(() => expect(arrayBuffer).toHaveBeenCalledTimes(2))
    const secondAbort = new DOMException("second load cancelled", "AbortError")
    secondController.abort(secondAbort)
    secondBody.reject(new Error("late body failure"))
    await expect(secondLoad).rejects.toBe(secondAbort)
    expect(live2dHarness.userModels).toHaveLength(0)
    second.dispose()
  })

  it("attempts every cleanup and retries only resources whose release failed", async () => {
    const OfficialCubismAvatar = await importAvatar()
    const host = createHost()
    const avatar = new OfficialCubismAvatar(host, "/avatar/model3.json", () => {
      throw new Error("fatal")
    })
    const internals = avatar as unknown as {
      model: { release: ReturnType<typeof vi.fn> } | null
      renderer: object | null
      gl: {
        deleteTexture: ReturnType<typeof vi.fn>
        getExtension: ReturnType<typeof vi.fn>
      } | null
      textures: WebGLTexture[]
      canvas: HTMLCanvasElement
      canvasRemoved: boolean
    }
    const modelError = new Error("model release failed")
    const release = vi
      .fn<() => void>()
      .mockImplementationOnce(() => {
        throw modelError
      })
    const deleteTexture = vi.fn()
    const firstTexture = {} as WebGLTexture
    const secondTexture = {} as WebGLTexture
    internals.model = { release }
    internals.renderer = {}
    internals.gl = {
      deleteTexture,
      getExtension: vi.fn(() => null),
    }
    internals.textures = [firstTexture, secondTexture]
    host.appendChild(internals.canvas)
    const remove = vi.spyOn(internals.canvas, "remove")

    expect(() => avatar.dispose()).toThrow(modelError)
    expect(deleteTexture).toHaveBeenCalledTimes(2)
    expect(remove).toHaveBeenCalledOnce()
    expect(internals.model).not.toBeNull()
    expect(internals.renderer).not.toBeNull()
    expect(internals.gl).toBeNull()
    expect(internals.textures).toEqual([])
    expect(internals.canvasRemoved).toBe(true)

    expect(() => avatar.dispose()).not.toThrow()
    expect(release).toHaveBeenCalledTimes(2)
    expect(deleteTexture).toHaveBeenCalledTimes(2)
    expect(remove).toHaveBeenCalledOnce()
    expect(internals.model).toBeNull()
    expect(internals.renderer).toBeNull()
  })

  it("retains only a texture whose delete failed and releases it on retry", async () => {
    const OfficialCubismAvatar = await importAvatar()
    const avatar = new OfficialCubismAvatar(
      createHost(),
      "/avatar/model3.json",
      () => {
        throw new Error("fatal")
      },
    )
    const internals = avatar as unknown as {
      gl: {
        deleteTexture: ReturnType<typeof vi.fn>
        getExtension: ReturnType<typeof vi.fn>
      } | null
      textures: WebGLTexture[]
    }
    const failedTexture = {} as WebGLTexture
    const releasedTexture = {} as WebGLTexture
    const deleteError = new Error("texture delete failed")
    const deleteTexture = vi.fn((texture: WebGLTexture) => {
      if (texture === failedTexture && deleteTexture.mock.calls.length === 1) {
        throw deleteError
      }
    })
    internals.gl = {
      deleteTexture,
      getExtension: vi.fn(() => null),
    }
    internals.textures = [failedTexture, releasedTexture]

    expect(() => avatar.dispose()).toThrow(deleteError)
    expect(internals.textures).toEqual([failedTexture])
    expect(internals.gl).not.toBeNull()

    expect(() => avatar.dispose()).not.toThrow()
    expect(internals.textures).toEqual([])
    expect(internals.gl).toBeNull()
    expect(deleteTexture).toHaveBeenCalledTimes(3)
  })

  it("aborts a pending image load without allowing its continuation to revive resources", async () => {
    live2dHarness.setting.textureCount = 1
    const gl = {
      deleteTexture: vi.fn(),
      getExtension: vi.fn(() => null),
    }
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(
      gl as unknown as RenderingContext,
    )
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        ok: true,
        arrayBuffer: async () => new ArrayBuffer(8),
      })),
    )

    const images: PendingImage[] = []
    class PendingImage {
      decoding = ""
      onload: (() => void) | null = null
      onerror: (() => void) | null = null
      private value = ""

      constructor() {
        images.push(this)
      }

      set src(value: string) {
        this.value = value
      }

      get src() {
        return this.value
      }
    }
    vi.stubGlobal("Image", PendingImage)

    const OfficialCubismAvatar = await importAvatar()
    const avatar = new OfficialCubismAvatar(
      createHost(),
      "/avatar/model3.json",
      () => {
        throw new Error("fatal")
      },
    )
    const internals = avatar as unknown as { canvas: HTMLCanvasElement }
    const loadPromise = avatar.load(new AbortController().signal)
    await vi.waitFor(() => expect(images).toHaveLength(1))

    avatar.dispose()

    await expect(loadPromise).rejects.toMatchObject({ name: "AbortError" })
    expect(hostContainsCanvas(internals.canvas)).toBe(false)
    expect(gl.deleteTexture).not.toHaveBeenCalled()
    expect(live2dHarness.userModels[0]?.release).toHaveBeenCalledOnce()
    expect(images[0]?.src).toBe("")
  })

  it("settles a throwing image src setup and removes every claimed handler", async () => {
    live2dHarness.setting.textureCount = 1
    const gl = {
      deleteTexture: vi.fn(),
      getExtension: vi.fn(() => null),
    }
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(
      gl as unknown as RenderingContext,
    )
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        ok: true,
        arrayBuffer: async () => new ArrayBuffer(8),
      })),
    )

    const setupError = new Error("image src setup failed")
    let savedLoad: (() => void) | null = null
    const images: ThrowingImage[] = []
    class ThrowingImage {
      decoding = ""
      onload: (() => void) | null = null
      onerror: (() => void) | null = null
      private value = ""

      constructor() {
        images.push(this)
      }

      set src(value: string) {
        this.value = value
        if (value) {
          savedLoad = this.onload
          throw setupError
        }
      }

      get src() {
        return this.value
      }
    }
    vi.stubGlobal("Image", ThrowingImage)

    const OfficialCubismAvatar = await importAvatar()
    const avatar = new OfficialCubismAvatar(
      createHost(),
      "/avatar/model3.json",
      () => {
        throw new Error("fatal")
      },
    )
    const loadPromise = avatar.load(new AbortController().signal)

    await expect(loadPromise).rejects.toBe(setupError)
    expect(images).toHaveLength(1)
    expect(images[0]?.onload).toBeNull()
    expect(images[0]?.onerror).toBeNull()
    expect(images[0]?.src).toBe("")

    savedLoad?.()
    await Promise.resolve()
    expect(gl.deleteTexture).not.toHaveBeenCalled()
    avatar.dispose()
  })

  it("stops image registration after a synchronous load-handler reentry", async () => {
    live2dHarness.setting.textureCount = 1
    const gl = {
      createTexture: vi.fn(() => null),
      getExtension: vi.fn(() => null),
    }
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(
      gl as unknown as RenderingContext,
    )
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        ok: true,
        arrayBuffer: async () => new ArrayBuffer(8),
      })),
    )

    let errorHandlerAssignments = 0
    let sourceAssignments = 0
    const controller = new AbortController()
    const addAbortListener = vi.spyOn(controller.signal, "addEventListener")
    let abortListenerRegistrationsAtImageConstruction: number | null = null
    class SynchronousLoadImage {
      decoding = ""
      private loadHandler: (() => void) | null = null

      constructor() {
        abortListenerRegistrationsAtImageConstruction = addAbortListener.mock.calls.length
      }

      set onload(value: (() => void) | null) {
        this.loadHandler = value
        value?.()
      }

      get onload() {
        return this.loadHandler
      }

      set onerror(_value: (() => void) | null) {
        errorHandlerAssignments += 1
      }

      get onerror() {
        return null
      }

      set src(value: string) {
        if (value) sourceAssignments += 1
      }

      get src() {
        return ""
      }
    }
    vi.stubGlobal("Image", SynchronousLoadImage)

    const OfficialCubismAvatar = await importAvatar()
    const avatar = new OfficialCubismAvatar(createHost(), "/avatar/model3.json", () => {
      throw new Error("fatal")
    })
    const internals = avatar as unknown as { imageLoadOperations: Set<unknown> }

    await expect(avatar.load(controller.signal)).rejects.toThrow(
      "Unable to create a WebGL texture",
    )
    expect(errorHandlerAssignments).toBe(0)
    expect(abortListenerRegistrationsAtImageConstruction).not.toBeNull()
    expect(addAbortListener).toHaveBeenCalledTimes(
      abortListenerRegistrationsAtImageConstruction!,
    )
    expect(sourceAssignments).toBe(0)
    expect(internals.imageLoadOperations.size).toBe(0)
    avatar.dispose()
  })

  it("preserves a null setup failure after a synchronous image load callback", async () => {
    live2dHarness.setting.textureCount = 1
    const gl = {
      createTexture: vi.fn(() => null),
      getExtension: vi.fn(() => null),
    }
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(
      gl as unknown as RenderingContext,
    )
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        ok: true,
        arrayBuffer: async () => new ArrayBuffer(8),
      })),
    )

    class SynchronousLoadThenNullFailureImage {
      decoding = ""
      private loadHandler: (() => void) | null = null
      onerror: (() => void) | null = null
      src = ""

      set onload(value: (() => void) | null) {
        this.loadHandler = value
        if (value) {
          value()
          throw null
        }
      }

      get onload() {
        return this.loadHandler
      }
    }
    vi.stubGlobal("Image", SynchronousLoadThenNullFailureImage)

    const OfficialCubismAvatar = await importAvatar()
    const avatar = new OfficialCubismAvatar(createHost(), "/avatar/model3.json", () => {
      throw new Error("fatal")
    })

    const failure = await avatar
      .load(new AbortController().signal)
      .catch((error: unknown) => error)
    expect(failure).toBeInstanceOf(AggregateError)
    expect((failure as AggregateError).errors).toContain(null)
    avatar.dispose()
  })

  it("rolls back a mutating image-handler assignment before any later registration", async () => {
    live2dHarness.setting.textureCount = 1
    const gl = { getExtension: vi.fn(() => null) }
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(
      gl as unknown as RenderingContext,
    )
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        ok: true,
        arrayBuffer: async () => new ArrayBuffer(8),
      })),
    )

    const setupError = new Error("image error handler mutated then failed")
    let sourceAssignments = 0
    const controller = new AbortController()
    const addAbortListener = vi.spyOn(controller.signal, "addEventListener")
    let abortListenerRegistrationsAtImageConstruction: number | null = null
    const images: MutatingHandlerImage[] = []
    class MutatingHandlerImage {
      decoding = ""
      onload: (() => void) | null = null
      private errorHandler: (() => void) | null = null

      constructor() {
        abortListenerRegistrationsAtImageConstruction = addAbortListener.mock.calls.length
        images.push(this)
      }

      set onerror(value: (() => void) | null) {
        this.errorHandler = value
        if (value) throw setupError
      }

      get onerror() {
        return this.errorHandler
      }

      set src(value: string) {
        if (value) sourceAssignments += 1
      }

      get src() {
        return ""
      }
    }
    vi.stubGlobal("Image", MutatingHandlerImage)

    const OfficialCubismAvatar = await importAvatar()
    const avatar = new OfficialCubismAvatar(createHost(), "/avatar/model3.json", () => {
      throw new Error("fatal")
    })

    await expect(avatar.load(controller.signal)).rejects.toBe(setupError)
    expect(images).toHaveLength(1)
    expect(images[0]?.onload).toBeNull()
    expect(images[0]?.onerror).toBeNull()
    expect(abortListenerRegistrationsAtImageConstruction).not.toBeNull()
    expect(addAbortListener).toHaveBeenCalledTimes(
      abortListenerRegistrationsAtImageConstruction!,
    )
    expect(sourceAssignments).toBe(0)
    avatar.dispose()
  })

  it("reports abort cleanup failure and blocks a late image continuation", async () => {
    live2dHarness.setting.textureCount = 1
    const gl = {
      deleteTexture: vi.fn(),
      getExtension: vi.fn(() => null),
    }
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(
      gl as unknown as RenderingContext,
    )
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        ok: true,
        arrayBuffer: async () => new ArrayBuffer(8),
      })),
    )

    const cleanupError = new Error("image request cancellation failed")
    let savedLoad: (() => void) | null = null
    const images: CleanupThrowingImage[] = []
    class CleanupThrowingImage {
      decoding = ""
      onload: (() => void) | null = null
      onerror: (() => void) | null = null
      private value = ""

      constructor() {
        images.push(this)
      }

      set src(value: string) {
        if (!value) throw cleanupError
        this.value = value
        savedLoad = this.onload
      }

      get src() {
        return this.value
      }
    }
    vi.stubGlobal("Image", CleanupThrowingImage)

    const OfficialCubismAvatar = await importAvatar()
    const avatar = new OfficialCubismAvatar(
      createHost(),
      "/avatar/model3.json",
      () => {
        throw new Error("fatal")
      },
    )
    const loadPromise = avatar.load(new AbortController().signal)
    const loadFailure = loadPromise.catch((error: unknown) => error)
    await vi.waitFor(() => expect(images).toHaveLength(1))

    expect(() => avatar.dispose()).toThrow(cleanupError)

    const failure = await loadFailure
    expect(failure).toBeInstanceOf(AggregateError)
    expect((failure as AggregateError).errors[0]).toMatchObject({
      name: "AbortError",
    })
    expect((failure as AggregateError).errors).toContain(cleanupError)
    expect(images[0]?.onload).toBeNull()
    expect(images[0]?.onerror).toBeNull()

    savedLoad?.()
    await Promise.resolve()
    expect(gl.deleteTexture).not.toHaveBeenCalled()
  })

  it("does not let a post-load cancellation continuation retry failed source cleanup", async () => {
    live2dHarness.setting.textureCount = 1
    const gl = {
      deleteTexture: vi.fn(),
      getExtension: vi.fn(() => null),
    }
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(
      gl as unknown as RenderingContext,
    )
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        ok: true,
        arrayBuffer: async () => new ArrayBuffer(8),
      })),
    )

    const cleanupError = new Error("loaded image source cleanup failed")
    let savedLoad: (() => void) | null = null
    let sourceCleanupAttempts = 0
    class RetryableSourceCleanupImage {
      decoding = ""
      onload: (() => void) | null = null
      onerror: (() => void) | null = null
      private value = ""

      set src(value: string) {
        if (!value) {
          sourceCleanupAttempts += 1
          if (sourceCleanupAttempts === 1) throw cleanupError
        }
        this.value = value
        if (value) savedLoad = this.onload
      }

      get src() {
        return this.value
      }
    }
    vi.stubGlobal("Image", RetryableSourceCleanupImage)

    const OfficialCubismAvatar = await importAvatar()
    const avatar = new OfficialCubismAvatar(createHost(), "/avatar/model3.json", () => {
      throw new Error("fatal")
    })
    const internals = avatar as unknown as { imageLoadOperations: Set<unknown> }
    const loadFailure = avatar
      .load(new AbortController().signal)
      .catch((error: unknown) => error)
    await vi.waitFor(() => expect(savedLoad).not.toBeNull())

    savedLoad?.()
    expect(() => avatar.dispose()).toThrow(cleanupError)

    const failure = await loadFailure
    expect(failure).toBeInstanceOf(AggregateError)
    expect((failure as AggregateError).errors[0]).toMatchObject({
      name: "AbortError",
    })
    expect((failure as AggregateError).errors).toContain(cleanupError)
    expect(sourceCleanupAttempts).toBe(1)
    expect(internals.imageLoadOperations.size).toBe(1)
    expect(gl.deleteTexture).not.toHaveBeenCalled()

    expect(() => avatar.dispose()).not.toThrow()
    expect(sourceCleanupAttempts).toBe(2)
    expect(internals.imageLoadOperations.size).toBe(0)
    expect(gl.deleteTexture).not.toHaveBeenCalled()
  })

  it("retains Core script handler cleanup debt and clears it before creating a retry", async () => {
    Object.defineProperty(window, "Live2DCubismCore", {
      configurable: true,
      writable: true,
      value: undefined,
    })
    const cleanupError = new Error("Core load handler cleanup failed")
    const scripts: HTMLScriptElement[] = []
    let firstLoadHandler: OnErrorEventHandler = null
    let firstNullAssignments = 0
    vi.spyOn(document.head, "appendChild").mockImplementation((node) => {
      const script = node as HTMLScriptElement
      scripts.push(script)
      if (scripts.length === 1) {
        firstLoadHandler = script.onload
        Object.defineProperty(script, "onload", {
          configurable: true,
          get: () => firstLoadHandler,
          set: (value: OnErrorEventHandler) => {
            if (value === null) {
              firstNullAssignments += 1
              if (firstNullAssignments === 1) throw cleanupError
            }
            firstLoadHandler = value
          },
        })
      }
      return node
    })

    const OfficialCubismAvatar = await importAvatar()
    const first = new OfficialCubismAvatar(createHost(), "/avatar/model3.json", () => {
      throw new Error("fatal")
    })
    const firstLoad = first.load(new AbortController().signal)
    await vi.waitFor(() => expect(scripts).toHaveLength(1))
    const firstScript = scripts[0]!
    firstScript.onerror?.(new Event("error"))

    const firstFailure = await firstLoad.catch((error: unknown) => error)
    expect(firstFailure).toBeInstanceOf(AggregateError)
    expect((firstFailure as AggregateError).errors).toContain(cleanupError)
    expect(firstNullAssignments).toBe(1)
    expect(firstScript.onerror).toBeNull()
    expect(firstScript.isConnected).toBe(false)
    first.dispose()

    const second = new OfficialCubismAvatar(createHost(), "/avatar/model3.json", () => {
      throw new Error("fatal")
    })
    const secondLoad = second.load(new AbortController().signal)
    await vi.waitFor(() => expect(scripts).toHaveLength(2))
    expect(firstNullAssignments).toBe(2)
    expect(firstScript.onload).toBeNull()

    const secondScript = scripts[1]!
    secondScript.onerror?.(new Event("error"))
    await expect(secondLoad).rejects.toThrow("Unable to load Live2D Cubism Core")
    second.dispose()
  })

  it("stops Core script registration after a synchronous load-handler reentry", async () => {
    Object.defineProperty(window, "Live2DCubismCore", {
      configurable: true,
      writable: true,
      value: undefined,
    })
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(null)

    const nativeCreateElement = document.createElement.bind(document)
    const script = nativeCreateElement("script")
    let loadHandler: OnErrorEventHandler = null
    let errorHandlerAssignments = 0
    Object.defineProperty(script, "onload", {
      configurable: true,
      get: () => loadHandler,
      set: (value: OnErrorEventHandler) => {
        loadHandler = value
        if (value) {
          window.Live2DCubismCore = {}
          value.call(script, new Event("load"))
        }
      },
    })
    Object.defineProperty(script, "onerror", {
      configurable: true,
      get: () => null,
      set: () => {
        errorHandlerAssignments += 1
      },
    })
    vi.spyOn(document, "createElement").mockImplementation(((tagName: string) => {
      if (tagName.toLowerCase() === "script") return script
      return nativeCreateElement(tagName)
    }) as typeof document.createElement)
    const appendChild = vi.spyOn(document.head, "appendChild")

    const OfficialCubismAvatar = await importAvatar()
    const avatar = new OfficialCubismAvatar(createHost(), "/avatar/model3.json", () => {
      throw new Error("fatal")
    })

    await expect(avatar.load(new AbortController().signal)).rejects.toThrow(
      "Unable to create a WebGL context for Live2D",
    )
    expect(errorHandlerAssignments).toBe(0)
    expect(appendChild).not.toHaveBeenCalled()
    expect(script.onload).toBeNull()
    avatar.dispose()
  })

  it("retains a texture whose transactional setup rollback failed and retries that exact texture", async () => {
    const setupError = new Error("texture upload failed")
    const deleteError = new Error("texture rollback failed")
    const texture = {} as WebGLTexture
    const deleteTexture = vi.fn((candidate: WebGLTexture) => {
      expect(candidate).toBe(texture)
      if (deleteTexture.mock.calls.length === 1) throw deleteError
    })
    const gl = {
      TEXTURE_2D: 1,
      UNPACK_PREMULTIPLY_ALPHA_WEBGL: 2,
      TEXTURE_MIN_FILTER: 3,
      LINEAR_MIPMAP_LINEAR: 4,
      TEXTURE_MAG_FILTER: 5,
      LINEAR: 6,
      TEXTURE_WRAP_S: 7,
      CLAMP_TO_EDGE: 8,
      TEXTURE_WRAP_T: 9,
      RGBA: 10,
      UNSIGNED_BYTE: 11,
      createTexture: vi.fn(() => texture),
      bindTexture: vi.fn(),
      pixelStorei: vi.fn(),
      texParameteri: vi.fn(),
      texImage2D: vi.fn(() => { throw setupError }),
      generateMipmap: vi.fn(),
      deleteTexture,
      getExtension: vi.fn(() => null),
    }

    const OfficialCubismAvatar = await importAvatar()
    const avatar = new OfficialCubismAvatar(createHost(), "/avatar/model3.json", () => {
      throw new Error("fatal")
    })
    const internals = avatar as unknown as {
      gl: typeof gl | null
      textures: WebGLTexture[]
      createTexture: (context: typeof gl, image: HTMLImageElement) => WebGLTexture
    }
    internals.gl = gl

    const failure = (() => {
      try {
        internals.createTexture(gl, {} as HTMLImageElement)
        return null
      } catch (error) {
        return error
      }
    })()
    expect(failure).toBeInstanceOf(AggregateError)
    expect((failure as AggregateError).errors).toEqual([setupError, deleteError])
    expect(internals.textures).toEqual([texture])
    expect(deleteTexture).toHaveBeenCalledOnce()

    expect(() => avatar.dispose()).not.toThrow()
    expect(internals.textures).toEqual([])
    expect(deleteTexture).toHaveBeenCalledTimes(2)
    expect(deleteTexture.mock.calls.every(([candidate]) => candidate === texture)).toBe(true)
  })

  it("retains image handler cleanup debt for a later dispose retry and blocks its late callback", async () => {
    live2dHarness.setting.textureCount = 1
    const gl = {
      deleteTexture: vi.fn(),
      getExtension: vi.fn(() => null),
    }
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(
      gl as unknown as RenderingContext,
    )
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        ok: true,
        arrayBuffer: async () => new ArrayBuffer(8),
      })),
    )

    const cleanupError = new Error("image load handler cleanup failed")
    let savedLoad: (() => void) | null = null
    let nullAssignments = 0
    class RetryableHandlerImage {
      decoding = ""
      private loadHandler: (() => void) | null = null
      onerror: (() => void) | null = null
      private value = ""

      set onload(value: (() => void) | null) {
        if (value === null) {
          nullAssignments += 1
          if (nullAssignments === 1) throw cleanupError
        }
        this.loadHandler = value
      }

      get onload() {
        return this.loadHandler
      }

      set src(value: string) {
        this.value = value
        if (value) savedLoad = this.loadHandler
      }

      get src() {
        return this.value
      }
    }
    vi.stubGlobal("Image", RetryableHandlerImage)

    const OfficialCubismAvatar = await importAvatar()
    const avatar = new OfficialCubismAvatar(createHost(), "/avatar/model3.json", () => {
      throw new Error("fatal")
    })
    const internals = avatar as unknown as { imageLoadOperations: Set<unknown> }
    const loadPromise = avatar.load(new AbortController().signal)
    const loadFailure = loadPromise.catch((error: unknown) => error)
    await vi.waitFor(() => expect(savedLoad).not.toBeNull())

    expect(() => avatar.dispose()).toThrow(cleanupError)
    const failure = await loadFailure
    expect(failure).toBeInstanceOf(AggregateError)
    expect((failure as AggregateError).errors).toContain(cleanupError)
    expect(internals.imageLoadOperations.size).toBe(1)
    expect(nullAssignments).toBe(1)

    savedLoad?.()
    await Promise.resolve()
    expect(gl.deleteTexture).not.toHaveBeenCalled()

    expect(() => avatar.dispose()).not.toThrow()
    expect(internals.imageLoadOperations.size).toBe(0)
    expect(nullAssignments).toBe(2)
    expect(gl.deleteTexture).not.toHaveBeenCalled()
  })

  it("resets a failed Core script load so a later avatar creates one fresh retry", async () => {
    Object.defineProperty(window, "Live2DCubismCore", {
      configurable: true,
      writable: true,
      value: undefined,
    })
    const scripts: HTMLScriptElement[] = []
    vi.spyOn(document.head, "appendChild").mockImplementation((node) => {
      scripts.push(node as HTMLScriptElement)
      return node
    })
    const OfficialCubismAvatar = await importAvatar()
    const first = new OfficialCubismAvatar(
      createHost(),
      "/avatar/model3.json",
      () => {
        throw new Error("fatal")
      },
    )
    const firstLoad = first.load(new AbortController().signal)
    await vi.waitFor(() => expect(scripts).toHaveLength(1))
    const firstScript = scripts[0]
    expect(firstScript).toBeDefined()
    firstScript?.onerror?.(new Event("error"))
    await expect(firstLoad).rejects.toThrow("Unable to load Live2D Cubism Core")
    first.dispose()
    expect(firstScript?.onload).toBeNull()
    expect(firstScript?.onerror).toBeNull()

    const second = new OfficialCubismAvatar(
      createHost(),
      "/avatar/model3.json",
      () => {
        throw new Error("fatal")
      },
    )
    const secondLoad = second.load(new AbortController().signal)
    await vi.waitFor(() => expect(scripts).toHaveLength(2))
    const secondScript = scripts[1]
    expect(secondScript).toBeDefined()
    expect(secondScript).not.toBe(firstScript)
    secondScript?.onerror?.(new Event("error"))
    await expect(secondLoad).rejects.toThrow("Unable to load Live2D Cubism Core")
    second.dispose()
  })
})

function hostContainsCanvas(canvas: HTMLCanvasElement) {
  return document.body.contains(canvas)
}
