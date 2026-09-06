import {
  createResourceOwner,
  type ResourceClaim,
  type ResourceOwner,
  type ResourceReleaseAttempt,
} from "@/lib/resource-owner"
import { createOwnedAnimationFrame } from "@/lib/timer-owner"

import { EXPRESSION_STATES, type ExpressionState } from "./expressionStates"

declare global {
  interface Window {
    Live2DCubismCore?: unknown
  }
}

const CUBISM_CORE_SRC = "/vendor/live2dcubismcore.min.js"
const CUBISM_SHADER_PATH = "/vendor/live2d/shaders/webgl/"
const AVATAR_VISIBLE_HEIGHT = 2.1
const AVATAR_TOP_Y = 0.85
const EXPRESSION_TRANSITION_MS = 450
const DEFAULT_EYE_OPEN_BASELINE = 0.8

type CubismRuntime = {
  framework: any
  ModelSettingJson: any
  Matrix44: any
  UserModel: any
}

let coreScriptPromise: Promise<void> | null = null
let coreScriptOwner: ResourceOwner | null = null
let frameworkPromise: Promise<CubismRuntime> | null = null
let cubismRuntime: CubismRuntime | null = null

type ImageLoadOperation = {
  owner: ResourceOwner
  cancel: (attempt: ResourceReleaseAttempt) => void
  abortAttempt: ResourceReleaseAttempt | null
  cleanupAttempt: ResourceReleaseAttempt | null
  cleanupFailed: boolean
  cleanupFailure: unknown
}

type CleanupResult =
  | { failed: false }
  | { failed: true; failure: unknown }

export class OfficialCubismAvatar {
  private readonly host: HTMLElement
  private readonly modelSrc: string
  private readonly onFatalError: (
    error: unknown,
    attempt: ResourceReleaseAttempt,
  ) => never
  private readonly canvas: HTMLCanvasElement
  private readonly lifetimeOwner = createResourceOwner("Live2D avatar lifetime")
  private gl: WebGLRenderingContext | WebGL2RenderingContext | null = null
  private model: any = null
  private renderer: any = null
  private runtime: CubismRuntime | null = null
  private textures: WebGLTexture[] = []
  private readonly imageLoadOperations = new Set<ImageLoadOperation>()
  private lifecycle: "idle" | "loading" | "ready" | "disposed" = "idle"
  private canvasCleared = false
  private canvasRemoved = false
  private mouthTarget = 0
  private mouthCurrent = 0
  private readonly parameterIds = new Map<string, any>()

  // Random phase offsets so each axis starts at different point in cycle
  private readonly phaseX = Math.random() * Math.PI * 2
  private readonly phaseY = Math.random() * Math.PI * 2
  private readonly phaseZ = Math.random() * Math.PI * 2
  private readonly phaseBody = Math.random() * Math.PI * 2
  private readonly phaseBreath = Math.random() * Math.PI * 2

  // Blink state
  private nextBlinkAt = performance.now() + 2000 + Math.random() * 3000
  private blinking = false
  private blinkStartAt = 0

  // Eye ball state — smooth random glances
  private eyeTargetX = 0
  private eyeTargetY = 0
  private eyeCurrentX = 0
  private eyeCurrentY = 0
  private nextEyeMoveAt = performance.now() + 1500 + Math.random() * 2000

  // Expression state (see expressionStates.ts) — pose/gesture targets driven
  // by the conversation phase, layered on top of the ambient motion above.
  private expressionState: ExpressionState = "idle"
  private expressionInitialized = false
  private readonly expressionCurrent = new Map<string, number>()
  private readonly expressionTransitions = new Map<string, {
    start: number
    target: number
    kind: "smooth" | "delayed"
    startedAt: number
  }>()
  // "EyeOpen" is withheld from the generic per-frame setParameter sweep below
  // and stored here instead — computeBlink() multiplies against it, so a
  // state's eyelid target (e.g. "thinking" squints to 0.8) is the resting
  // position the blink still dips down from and returns to.
  private eyeOpenBaseline = DEFAULT_EYE_OPEN_BASELINE

  constructor(
    host: HTMLElement,
    modelSrc: string,
    onFatalError: (
      error: unknown,
      attempt: ResourceReleaseAttempt,
    ) => never,
  ) {
    this.host = host
    this.modelSrc = modelSrc
    this.onFatalError = onFatalError
    this.canvas = document.createElement("canvas")
    this.canvas.className = "absolute inset-0 h-full w-full"
  }

  async load(signal: AbortSignal) {
    if (this.lifecycle !== "idle") {
      throw new Error("Live2D avatar instances are single-use")
    }
    this.lifecycle = "loading"
    const loadSignal = AbortSignal.any([signal, this.lifetimeOwner.signal])

    this.throwIfLoadCancelled(loadSignal)
    // Claim DOM ownership before appendChild(): an imperative DOM implementation
    // may mutate successfully and still throw. The component already owns this
    // avatar instance, so its single rollback path can always call dispose().
    this.canvasRemoved = false
    this.host.appendChild(this.canvas)

    this.throwIfLoadCancelled(loadSignal)
    const runtime = await ensureCubismFramework()
    this.throwIfLoadCancelled(loadSignal)
    this.runtime = runtime
    this.resize()

    this.gl = createWebGlContext(this.canvas)
    if (!this.gl) throw new Error("Unable to create a WebGL context for Live2D")

    const modelUrl = new URL(this.modelSrc, window.location.href)
    const modelRootUrl = new URL(".", modelUrl)
    const settingBuffer = await fetchArrayBuffer(modelUrl, loadSignal)
    this.throwIfLoadCancelled(loadSignal)
    const setting = new runtime.ModelSettingJson(
      settingBuffer,
      settingBuffer.byteLength,
    )

    const mocUrl = new URL(setting.getModelFileName(), modelRootUrl)
    const mocBuffer = await fetchArrayBuffer(mocUrl, loadSignal)
    this.throwIfLoadCancelled(loadSignal)

    this.model = new runtime.UserModel()
    this.model.loadModel(mocBuffer, false)
    this.applyModelLayout(setting)

    this.model.createRenderer(this.canvas.width, this.canvas.height, 1)
    this.renderer = this.model.getRenderer()
    if (!this.renderer) throw new Error("Unable to create a Live2D renderer")
    this.renderer.startUp(this.gl)
    this.renderer.setIsPremultipliedAlpha(true)

    await this.loadTextures(setting, modelRootUrl, loadSignal)
    this.throwIfLoadCancelled(loadSignal)
    this.lifecycle = "ready"
    this.setExpressionState("idle")
    this.start()
  }

  setMouthAmplitude(value: number) {
    if (this.lifecycle === "disposed") return
    this.mouthTarget = value
  }

  /**
   * Switch the avatar's pose/expression. Safe to call before load() finishes —
   * it only queues target values into expressionCurrent/expressionTransitions;
   * the actual setParameter() calls happen every frame from updateParameters().
   */
  setExpressionState(next: ExpressionState) {
    if (this.lifecycle === "disposed") return
    if (this.expressionInitialized && this.expressionState === next) return
    this.expressionState = next

    const now = performance.now()
    for (const [id, target] of Object.entries(EXPRESSION_STATES[next])) {
      // Before the first pose is applied there's nothing to ease from — snap
      // straight to the target instead of animating in from an undefined
      // (or raw model-default) current value.
      if (!this.expressionInitialized || target.transition === "instant") {
        this.expressionCurrent.set(id, target.value)
        this.expressionTransitions.delete(id)
        continue
      }

      const start = this.expressionCurrent.get(id) ?? target.value
      this.expressionTransitions.set(id, {
        start,
        target: target.value,
        kind: target.transition,
        startedAt: now,
      })
    }

    this.expressionInitialized = true
  }

  resize() {
    if (this.lifecycle === "disposed") return
    const rect = this.host.getBoundingClientRect()
    if (!rect.width || !rect.height) return

    const scale = window.devicePixelRatio || 1
    const width = Math.max(1, Math.floor(rect.width * scale))
    const height = Math.max(1, Math.floor(rect.height * scale))

    if (this.canvas.width !== width || this.canvas.height !== height) {
      this.canvas.width = width
      this.canvas.height = height
      this.model?.setRenderTargetSize(width, height)
    }
  }

  dispose(attempt: ResourceReleaseAttempt = {}) {
    this.lifecycle = "disposed"
    const errors: unknown[] = []

    for (const operation of this.imageLoadOperations) {
      operation.abortAttempt = attempt
    }
    try {
      this.lifetimeOwner.dispose(attempt)
    } catch (error) {
      errors.push(error)
    }
    this.parameterIds.clear()
    this.expressionCurrent.clear()
    this.expressionTransitions.clear()

    for (const operation of [...this.imageLoadOperations]) {
      try {
        operation.cancel(attempt)
      } catch (error) {
        errors.push(error)
      } finally {
        operation.abortAttempt = null
      }
    }

    const gl = this.gl
    if (gl && this.textures.length) {
      const remainingTextures: WebGLTexture[] = []
      for (const texture of this.textures) {
        try {
          gl.deleteTexture(texture)
        } catch (error) {
          remainingTextures.push(texture)
          errors.push(error)
        }
      }
      this.textures = remainingTextures
    }

    if (this.model) {
      try {
        this.model.release()
        this.model = null
        this.renderer = null
      } catch (error) {
        errors.push(error)
      }
    } else {
      this.renderer = null
    }

    if (gl) {
      try {
        const contextLoss = gl.getExtension("WEBGL_lose_context")
        if (contextLoss) {
          contextLoss.loseContext()
          this.textures = []
        }
        if (!this.textures.length) this.gl = null
      } catch (error) {
        errors.push(error)
      }
    }

    if (!this.canvasCleared) {
      try {
        this.canvas.width = 0
        this.canvas.height = 0
        this.canvasCleared = true
      } catch (error) {
        errors.push(error)
      }
    }

    if (!this.canvasRemoved) {
      try {
        this.canvas.remove()
        this.canvasRemoved = true
      } catch (error) {
        errors.push(error)
      }
    }

    if (errors.length === 1) throw errors[0]
    if (errors.length > 1) {
      throw new AggregateError(
        errors,
        "Failed to release all Live2D avatar resources",
      )
    }
  }

  private throwIfLoadCancelled(signal: AbortSignal) {
    if (this.lifecycle === "disposed") {
      throw new DOMException("Live2D avatar load aborted", "AbortError")
    }
    signal.throwIfAborted()
  }

  private applyModelLayout(setting: any) {
    const modelMatrix = this.model.getModelMatrix()
    const layout = new Map<string, number>()

    if (setting.getLayoutMap(layout)) {
      modelMatrix.setupFromLayout(layout)
      return
    }

    const cubismModel = this.model.getModel()
    cubismModel.update()

    const bounds = getDrawableBounds(cubismModel)
    const height = bounds ? bounds.maxY - bounds.minY : cubismModel.getCanvasHeight()
    const centerX = bounds ? (bounds.minX + bounds.maxX) / 2 : cubismModel.getCanvasWidth() / 2
    const centerY = bounds ? (bounds.minY + bounds.maxY) / 2 : cubismModel.getCanvasHeight() / 2
    const scale = AVATAR_VISIBLE_HEIGHT / height
    const targetCenterY = AVATAR_TOP_Y - AVATAR_VISIBLE_HEIGHT / 2

    modelMatrix.loadIdentity()
    modelMatrix.scale(scale, scale)
    modelMatrix.translate(-centerX * scale, -centerY * scale + targetCenterY)
  }

  private async loadTextures(setting: any, modelRootUrl: URL, signal: AbortSignal) {
    const textureDirectory = setting.getTextureDirectory()
    const gl = this.gl
    const renderer = this.renderer
    if (!gl || !renderer) throw new Error("Live2D renderer is not initialized")

    for (let index = 0; index < setting.getTextureCount(); index += 1) {
      const textureFileName = setting.getTextureFileName(index)
      const texturePath = textureFileName.includes("/") || !textureDirectory
        ? textureFileName
        : `${textureDirectory}/${textureFileName}`
      const image = await loadImage(
        new URL(texturePath, modelRootUrl),
        signal,
        this.imageLoadOperations,
      )
      signal.throwIfAborted()
      const texture = this.createTexture(gl, image)

      renderer.bindTexture(index, texture)
    }
  }

  private createTexture(
    gl: WebGLRenderingContext | WebGL2RenderingContext,
    image: HTMLImageElement,
  ) {
    const texture = gl.createTexture()
    if (!texture) throw new Error("Unable to create a WebGL texture")

    // The avatar becomes the authoritative texture owner before the first GL
    // mutation. A rollback failure therefore remains retryable via dispose().
    this.textures.push(texture)
    try {
      gl.bindTexture(gl.TEXTURE_2D, texture)
      gl.pixelStorei(gl.UNPACK_PREMULTIPLY_ALPHA_WEBGL, 1)
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR_MIPMAP_LINEAR)
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR)
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE)
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE)
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, image)
      gl.generateMipmap(gl.TEXTURE_2D)
      gl.bindTexture(gl.TEXTURE_2D, null)
      return texture
    } catch (error) {
      const cleanupErrors: unknown[] = []
      try {
        gl.bindTexture(gl.TEXTURE_2D, null)
      } catch (cleanupError) {
        cleanupErrors.push(cleanupError)
      }
      try {
        gl.deleteTexture(texture)
        const index = this.textures.indexOf(texture)
        if (index >= 0) this.textures.splice(index, 1)
      } catch (cleanupError) {
        cleanupErrors.push(cleanupError)
      }
      if (cleanupErrors.length) {
        throw new AggregateError(
          [error, ...cleanupErrors],
          "Failed to create and roll back a Live2D texture",
        )
      }
      throw error
    }
  }

  private start() {
    // Render once synchronously so initial renderer failures remain part of the
    // transactional load() boundary and trigger its rollback.
    this.render()

    const scheduleNext = (acquisitionAttempt: ResourceReleaseAttempt) => {
      createOwnedAnimationFrame(
        this.lifetimeOwner,
        "Live2D render frame",
        (_timestamp, callbackAttempt) => {
          if (this.lifetimeOwner.disposed) return

          try {
            this.render()
            if (!this.lifetimeOwner.disposed) {
              scheduleNext(callbackAttempt)
            }
          } catch (error) {
            // Propagate the physical callback's exact release attempt through
            // the component owner. If teardown fails, the timer helper joins
            // the same attempt without replaying an already-failed claim.
            this.onFatalError(error, callbackAttempt)
          }
        },
        acquisitionAttempt,
      )
    }

    scheduleNext({})
  }

  private render() {
    if (!this.gl || !this.model || !this.renderer) return

    // Sizing is driven by the host's ResizeObserver (see Live2DAvatar.vue), not
    // measured here — calling resize() every frame forced a getBoundingClientRect()
    // layout read at ~60fps for no benefit, since the host size only changes on
    // actual resize events.
    this.updateParameters()

    const cubismModel = this.model.getModel()
    cubismModel.update()

    this.gl.clearColor(0, 0, 0, 0)
    this.gl.clear(this.gl.COLOR_BUFFER_BIT)

    const runtime = this.runtime
    if (!runtime) throw new Error("Live2D framework is not initialized")
    const projection = new runtime.Matrix44()
    const width = this.canvas.width
    const height = this.canvas.height

    if (width > height) {
      projection.scale(1, width / height)
    }
    else {
      projection.scale(height / width, 1)
    }

    projection.multiplyByMatrix(this.model.getModelMatrix())
    this.renderer.setMvpMatrix(projection)
    this.renderer.setRenderState(null, [0, 0, width, height])
    this.renderer.drawModel(CUBISM_SHADER_PATH)
  }

  private updateParameters() {
    const now = performance.now()
    const t = now / 1000

    // Expression pose (eyebrows, mouth shape, arm/hand gestures, eyelid
    // baseline) — layered underneath the ambient motion below.
    this.updateExpressionTransitions(now)

    // Compound sine waves at incommensurate frequencies → aperiodic, organic drift
    const angleX = Math.sin(t * 0.53 + this.phaseX) * 3.5 + Math.sin(t * 1.21 + this.phaseX * 1.3) * 1.2
    const angleY = Math.sin(t * 0.67 + this.phaseY) * 1.5 + Math.sin(t * 1.47 + this.phaseY * 1.7) * 0.6
    const angleZ = Math.sin(t * 0.31 + this.phaseZ) * 1.5 + Math.sin(t * 0.79 + this.phaseZ * 0.9) * 0.6
    const bodyAngleX = Math.sin(t * 0.28 + this.phaseBody) * 1.8 + Math.sin(t * 0.63 + this.phaseBody * 1.4) * 0.6

    this.setParameter("ParamAngleX", angleX)
    this.setParameter("ParamAngleY", angleY)
    this.setParameter("ParamAngleZ", angleZ)
    this.setParameter("ParamBodyAngleX", bodyAngleX)

    // Breathing — slow ~7.5 s cycle, maps to 0–1
    const breath = 0.5 + 0.5 * Math.sin(t * 0.84 + this.phaseBreath)
    this.setParameter("ParamBreath", breath)

    // Random-interval blink with smooth eyelid curve. computeBlink() returns
    // a 0–1 "how open" fraction relative to fully open; multiplying it by the
    // expression state's eyeOpenBaseline (e.g. "thinking" squints to 0.8)
    // keeps the blink dipping to fully closed and back to that pose's resting
    // openness, instead of always resting at 1.
    const blink = this.computeBlink(now)
    this.setParameter("EyeOpen", this.eyeOpenBaseline * blink)

    // Eye ball — random glances every 2–6 s, 30% chance to return to center
    if (now >= this.nextEyeMoveAt) {
      if (Math.random() < 0.3) {
        this.eyeTargetX = 0
        this.eyeTargetY = 0
      }
      else {
        this.eyeTargetX = (Math.random() - 0.5) * 1.2
        this.eyeTargetY = (Math.random() - 0.5) * 0.8
      }
      this.nextEyeMoveAt = now + 2000 + Math.random() * 4000
    }
    this.eyeCurrentX += (this.eyeTargetX - this.eyeCurrentX) * 0.06
    this.eyeCurrentY += (this.eyeTargetY - this.eyeCurrentY) * 0.06
    this.setParameter("ParamEyeBallX", this.eyeCurrentX)
    this.setParameter("ParamEyeBallY", this.eyeCurrentY)

    // Lerp toward target amplitude for smooth response
    this.mouthCurrent += (this.mouthTarget - this.mouthCurrent) * 0.4
    this.setParameter("ParamMouthOpenY", this.mouthCurrent)
  }

  /**
   * Advance and apply the pose/gesture transitions queued by
   * setExpressionState(). "EyeOpen" is redirected into eyeOpenBaseline
   * instead of being written directly — see the comment at its field
   * declaration.
   */
  private updateExpressionTransitions(now: number) {
    for (const [id, transition] of this.expressionTransitions) {
      const progress = Math.min((now - transition.startedAt) / EXPRESSION_TRANSITION_MS, 1)

      if (transition.kind === "delayed") {
        if (progress < 1) continue
        this.expressionCurrent.set(id, transition.target)
        this.expressionTransitions.delete(id)
        continue
      }

      const eased = this.easeInOut(progress)
      const value = transition.start + (transition.target - transition.start) * eased
      this.expressionCurrent.set(id, value)
      if (progress >= 1) this.expressionTransitions.delete(id)
    }

    for (const [id, value] of this.expressionCurrent) {
      if (id === "EyeOpen") {
        this.eyeOpenBaseline = value
        continue
      }
      this.setParameter(id, value)
    }
  }

  private easeInOut(progress: number): number {
    if (progress < 0.5) return 2 * progress * progress
    return 1 - Math.pow(-2 * progress + 2, 2) / 2
  }

  /**
   * Smooth blink with randomised intervals (3–7 s).
   * Close phase: 80 ms, open phase: 120 ms.
   */
  private computeBlink(now: number): number {
    if (!this.blinking && now >= this.nextBlinkAt) {
      this.blinking = true
      this.blinkStartAt = now
    }

    if (!this.blinking) return 1

    const elapsed = now - this.blinkStartAt
    const CLOSE_MS = 80
    const OPEN_MS = 120

    if (elapsed >= CLOSE_MS + OPEN_MS) {
      this.blinking = false
      this.nextBlinkAt = now + 3000 + Math.random() * 4000
      return 1
    }

    // Smooth close → open using cosine easing
    if (elapsed < CLOSE_MS) {
      return 0.5 + 0.5 * Math.cos((elapsed / CLOSE_MS) * Math.PI)
    }
    return 0.5 - 0.5 * Math.cos(((elapsed - CLOSE_MS) / OPEN_MS) * Math.PI)
  }

  private setParameter(name: string, value: number) {
    const cubismModel = this.model.getModel()
    let id = this.parameterIds.get(name)

    if (!id) {
      const runtime = this.runtime
      if (!runtime) throw new Error("Live2D framework is not initialized")
      id = runtime.framework.getIdManager().getId(name)
      this.parameterIds.set(name, id)
    }

    cubismModel.setParameterValueById(id, value)
  }
}

async function ensureCubismFramework(): Promise<CubismRuntime> {
  if (cubismRuntime) return cubismRuntime

  if (!frameworkPromise) {
    let operation!: Promise<CubismRuntime>
    operation = Promise.resolve()
      .then(() => ensureCubismCore())
      .then(async () => {
        const [
          frameworkModule,
          settingModule,
          matrixModule,
          userModelModule,
        ] = await Promise.all([
          import("@/vendor/live2d/framework/live2dcubismframework.js"),
          import("@/vendor/live2d/framework/cubismmodelsettingjson.js"),
          import("@/vendor/live2d/framework/math/cubismmatrix44.js"),
          import("@/vendor/live2d/framework/model/cubismusermodel.js"),
        ])

        const framework = frameworkModule.CubismFramework
        const wasStarted = framework.isStarted()
        const wasInitialized = framework.isInitialized()
        let startupRollbackRequired = false
        let initializationRollbackRequired = false
        try {
          if (!wasStarted) {
            const option = new frameworkModule.Option()
            option.logFunction = () => undefined
            option.loggingLevel = frameworkModule.LogLevel.LogLevel_Off
            startupRollbackRequired = true
            if (framework.startUp(option) !== true) {
              throw new Error("Live2D Cubism Framework failed to start")
            }
          }

          if (!wasInitialized) {
            initializationRollbackRequired = true
            framework.initialize()
          }
        } catch (error) {
          const rollbackErrors: unknown[] = []
          if (initializationRollbackRequired) {
            try {
              framework.dispose()
            } catch (rollbackError) {
              rollbackErrors.push(rollbackError)
            }
          }
          if (startupRollbackRequired) {
            try {
              framework.cleanUp()
            } catch (rollbackError) {
              rollbackErrors.push(rollbackError)
            }
          }
          if (rollbackErrors.length > 0) {
            throw new AggregateError(
              [error, ...rollbackErrors],
              "Failed to initialize and roll back Live2D Cubism Framework",
            )
          }
          throw error
        }

        const runtime: CubismRuntime = {
          framework,
          ModelSettingJson: settingModule.CubismModelSettingJson,
          Matrix44: matrixModule.CubismMatrix44,
          UserModel: userModelModule.CubismUserModel,
        }
        cubismRuntime = runtime
        return runtime
      })
      .catch((error) => {
        if (frameworkPromise === operation) frameworkPromise = null
        throw error
      })
    frameworkPromise = operation
  }

  return frameworkPromise
}

async function ensureCubismCore() {
  if (coreScriptPromise) {
    await coreScriptPromise
    return
  }

  // A previous failed release remains the authoritative owner. It must be
  // discharged before either accepting an already-loaded Core or acquiring a
  // fresh script element, otherwise retries can accumulate handlers/nodes.
  releaseCoreScriptOwner()
  if (window.Live2DCubismCore) return

  let operation!: Promise<void>
  operation = Promise.resolve()
    .then(() => {
      if (window.Live2DCubismCore) return
      return loadCubismCoreScript()
    })
    .catch((error) => {
      if (coreScriptPromise === operation) coreScriptPromise = null
      throw error
    })
  coreScriptPromise = operation
  await operation
}

function releaseCoreScriptOwner(
  expected?: ResourceOwner,
  attempt: ResourceReleaseAttempt = {},
) {
  const owner = coreScriptOwner
  if (!owner || (expected && owner !== expected)) return
  owner.dispose(attempt)
  if (coreScriptOwner === owner && owner.settled) coreScriptOwner = null
}

function loadCubismCoreScript(): Promise<void> {
  const script = document.createElement("script")
  const owner = createResourceOwner("Live2D Cubism Core script")
  const setupAttempt: ResourceReleaseAttempt = {}
  coreScriptOwner = owner

  let resolvePending!: () => void
  let rejectPending!: (error: unknown) => void
  const pending = new Promise<void>((resolve, reject) => {
    resolvePending = resolve
    rejectPending = reject
  })
  let setupComplete = false
  let terminalSettled = false
  const terminalState: {
    result: { ok: true } | { ok: false; error: unknown } | null
  } = { result: null }

  const settleTerminalResult = () => {
    if (!setupComplete || terminalSettled || terminalState.result === null) return
    terminalSettled = true
    const result = terminalState.result

    let cleanupResult: CleanupResult = { failed: false }
    try {
      releaseCoreScriptOwner(owner, setupAttempt)
    } catch (error) {
      cleanupResult = { failed: true, failure: error }
    }

    if (result.ok) {
      if (!cleanupResult.failed) resolvePending()
      else {
        rejectPending(new AggregateError(
          [cleanupResult.failure],
          "Live2D Cubism Core loaded but its script resources were not released",
        ))
      }
      return
    }

    if (!cleanupResult.failed) rejectPending(result.error)
    else {
      rejectPending(new AggregateError(
        [result.error, cleanupResult.failure],
        "Failed to load and roll back Live2D Cubism Core",
      ))
    }
  }
  const finish = (result: { ok: true } | { ok: false; error: unknown }) => {
    if (terminalState.result !== null) return
    terminalState.result = result
    settleTerminalResult()
  }

  const onLoad = () => {
    if (window.Live2DCubismCore) finish({ ok: true })
    else {
      finish({
        ok: false,
        error: new Error("Live2D Cubism Core did not initialize"),
      })
    }
  }
  const onError = () => {
    finish({
      ok: false,
      error: new Error("Unable to load Live2D Cubism Core"),
    })
  }

  try {
    owner.acquire(
      () => { script.onload = onLoad },
      () => { script.onload = null },
      "Core script load callback",
      setupAttempt,
    )
    if (terminalState.result === null) {
      owner.acquire(
        () => { script.onerror = onError },
        () => { script.onerror = null },
        "Core script error callback",
        setupAttempt,
      )
    }
    if (terminalState.result === null) script.src = CUBISM_CORE_SRC
    if (terminalState.result === null) script.async = true
    if (terminalState.result === null) {
      owner.acquire(
        () => document.head.appendChild(script),
        () => script.remove(),
        "Core script element",
        setupAttempt,
      )
    }
  } catch (setupError) {
    const interruptedResult = terminalState.result
    const setupFailures: unknown[] = []
    if (interruptedResult !== null && !interruptedResult.ok) {
      setupFailures.push(interruptedResult.error)
    }
    setupFailures.push(setupError)
    terminalState.result = {
      ok: false,
      error: interruptedResult === null
        ? setupError
        : new AggregateError(
            setupFailures,
            "Live2D Cubism Core terminated while script setup was still mutating",
          ),
    }
  } finally {
    setupComplete = true
    settleTerminalResult()
  }

  return pending
}

function createWebGlContext(canvas: HTMLCanvasElement) {
  const options: WebGLContextAttributes = {
    alpha: true,
    antialias: true,
    premultipliedAlpha: true,
    preserveDrawingBuffer: false,
  }

  return canvas.getContext("webgl2", options) ?? canvas.getContext("webgl", options)
}

async function fetchArrayBuffer(url: URL, signal: AbortSignal) {
  signal.throwIfAborted()
  let response: Response
  try {
    response = await fetch(url, { signal })
    signal.throwIfAborted()
  } catch (error) {
    signal.throwIfAborted()
    throw error
  }
  if (!response.ok) throw new Error(`Unable to load ${url.pathname}`)

  try {
    signal.throwIfAborted()
    const buffer = await response.arrayBuffer()
    signal.throwIfAborted()
    return buffer
  } catch (error) {
    signal.throwIfAborted()
    throw error
  }
}

async function loadImage(
  url: URL,
  signal: AbortSignal,
  operations: Set<ImageLoadOperation>,
) {
  const image = new Image()
  const owner = createResourceOwner(`Live2D image ${url.pathname}`)
  const setupAttempt: ResourceReleaseAttempt = {}
  let cancelOperation = (_attempt: ResourceReleaseAttempt) => undefined
  const operation: ImageLoadOperation = {
    owner,
    cancel: attempt => cancelOperation(attempt),
    abortAttempt: null,
    cleanupAttempt: null,
    cleanupFailed: false,
    cleanupFailure: undefined,
  }
  operations.add(operation)

  const releaseOperation = (
    attempt: ResourceReleaseAttempt,
    retainedFailures: readonly unknown[] = [],
  ): CleanupResult => {
    if (operation.cleanupAttempt === attempt) {
      return operation.cleanupFailed
        ? { failed: true, failure: operation.cleanupFailure }
        : { failed: false }
    }
    operation.cleanupAttempt = attempt

    const cleanupFailures: unknown[] = []
    try {
      owner.dispose(attempt)
    } catch (error) {
      cleanupFailures.push(error)
    }

    if (owner.settled) {
      operation.cleanupFailed = false
      operation.cleanupFailure = undefined
      operations.delete(operation)
    } else {
      if (cleanupFailures.length === 0) cleanupFailures.push(...retainedFailures)
      if (cleanupFailures.length === 0) {
        cleanupFailures.push(new Error(
          `Image ${url.pathname} cleanup left unresolved resources`,
        ))
      }
      operation.cleanupFailed = true
      operation.cleanupFailure = cleanupFailures.length === 1
        ? cleanupFailures[0]
        : new AggregateError(
            cleanupFailures,
            `Image ${url.pathname} cleanup reported multiple failures`,
          )
    }
    return operation.cleanupFailed
      ? { failed: true, failure: operation.cleanupFailure }
      : { failed: false }
  }

  const sourceState: { claim: ResourceClaim | null } = { claim: null }
  const loaded = new Promise<void>((resolve, reject) => {
    type TerminalResult =
      | { ok: true; attempt: ResourceReleaseAttempt }
      | {
          ok: false
          error: unknown
          message: string
          attempt: ResourceReleaseAttempt
        }

    const terminalState: { result: TerminalResult | null } = { result: null }
    let terminalSettled = false
    let setupComplete = false
    let loadClaim: ResourceClaim | null = null
    let errorClaim: ResourceClaim | null = null
    let abortClaim: ResourceClaim | null = null

    const releaseClaims = (
      claims: readonly (ResourceClaim | null)[],
      attempt: ResourceReleaseAttempt,
    ) => {
      const errors: unknown[] = []
      for (const claim of claims) {
        if (!claim) continue
        try {
          claim.release(attempt)
        } catch (error) {
          errors.push(error)
        }
      }
      return errors
    }

    const settleTerminalResult = () => {
      if (!setupComplete || terminalSettled || terminalState.result === null) return
      terminalSettled = true
      const result = terminalState.result

      if (!result.ok) {
        const cleanupResult = releaseOperation(result.attempt, [result.error])
        if (cleanupResult.failed && cleanupResult.failure !== result.error) {
          reject(new AggregateError([result.error, cleanupResult.failure], result.message))
        } else {
          reject(result.error)
        }
        return
      }

      const cleanupErrors: unknown[] = []
      try {
        owner.close(undefined, result.attempt)
      } catch (error) {
        cleanupErrors.push(error)
      }
      cleanupErrors.push(
        ...releaseClaims([loadClaim, errorClaim, abortClaim], result.attempt),
      )

      if (cleanupErrors.length > 0) {
        const cleanupFailure = new AggregateError(
          cleanupErrors,
          `Loaded ${url.pathname} but failed to release image-load resources`,
        )
        const releaseResult = releaseOperation(result.attempt, [cleanupFailure])
        if (releaseResult.failed && releaseResult.failure !== cleanupFailure) {
          reject(new AggregateError(
            [cleanupFailure, releaseResult.failure],
            `Loaded ${url.pathname} but image rollback also failed`,
          ))
        } else {
          reject(cleanupFailure)
        }
        return
      }

      if (owner.settled) operations.delete(operation)
      resolve()
    }

    const requestSuccess = (attempt: ResourceReleaseAttempt) => {
      if (terminalState.result !== null) return
      if (signal.aborted) {
        requestFailure(
          new DOMException("Live2D image load aborted", "AbortError"),
          `Failed to abort and release image ${url.pathname}`,
          attempt,
        )
        return
      }
      terminalState.result = { ok: true, attempt }
      settleTerminalResult()
    }

    const requestFailure = (
      error: unknown,
      message: string,
      attempt: ResourceReleaseAttempt,
    ) => {
      if (terminalState.result !== null) return
      terminalState.result = { ok: false, error, message, attempt }
      settleTerminalResult()
    }

    const abortError = () => new DOMException(
      "Live2D image load aborted",
      "AbortError",
    )
    const abort = () => {
      requestFailure(
        abortError(),
        `Failed to abort and release image ${url.pathname}`,
        operation.abortAttempt ?? setupAttempt,
      )
    }
    cancelOperation = (attempt) => {
      requestFailure(
        abortError(),
        `Failed to abort and release image ${url.pathname}`,
        attempt,
      )
      if (!terminalSettled) return
      const cleanupResult = releaseOperation(attempt)
      if (cleanupResult.failed) throw cleanupResult.failure
    }
    const load = () => requestSuccess(setupAttempt)
    const loadError = () => {
      requestFailure(
        new Error(`Unable to load ${url.pathname}`),
        `Failed to load and release image ${url.pathname}`,
        setupAttempt,
      )
    }

    try {
      image.decoding = "async"
      loadClaim = owner.acquire(
        () => { image.onload = load },
        () => { image.onload = null },
        "image load callback",
        setupAttempt,
      )
      if (terminalState.result === null) {
        errorClaim = owner.acquire(
          () => { image.onerror = loadError },
          () => { image.onerror = null },
          "image error callback",
          setupAttempt,
        )
      }
      if (terminalState.result === null) {
        abortClaim = owner.acquire(
          () => signal.addEventListener("abort", abort),
          () => signal.removeEventListener("abort", abort),
          "image abort listener",
          setupAttempt,
        )
      }
      if (terminalState.result === null && signal.aborted) abort()
      if (terminalState.result === null) {
        sourceState.claim = owner.claim(
          "image request source",
          () => { image.src = "" },
        )
        image.src = url.href
      }
    } catch (setupError) {
      const interruptedResult = terminalState.result
      const setupFailure = interruptedResult === null
        ? setupError
        : new AggregateError(
            interruptedResult.ok
              ? [setupError]
              : [interruptedResult.error, setupError],
            `Image ${url.pathname} terminated while setup was still mutating`,
          )
      terminalState.result = {
        ok: false,
        error: setupFailure,
        message: `Failed to initialize and release image ${url.pathname}`,
        attempt: setupAttempt,
      }
    } finally {
      setupComplete = true
      settleTerminalResult()
    }
  })

  await loaded
  try {
    signal.throwIfAborted()
  } catch (error) {
    if (operation.cleanupFailed) {
      throw new AggregateError(
        [error, operation.cleanupFailure],
        `Loaded ${url.pathname} but cancellation cleanup remains unresolved`,
      )
    }

    const cleanupResult = releaseOperation(setupAttempt, [error])
    if (cleanupResult.failed && cleanupResult.failure !== error) {
      throw new AggregateError(
        [error, cleanupResult.failure],
        `Loaded ${url.pathname} but failed to roll back after cancellation`,
      )
    }
    throw error
  }

  sourceState.claim?.transfer()
  if (owner.settled) operations.delete(operation)
  return image
}

function getDrawableBounds(cubismModel: any) {
  let minX = Number.POSITIVE_INFINITY
  let minY = Number.POSITIVE_INFINITY
  let maxX = Number.NEGATIVE_INFINITY
  let maxY = Number.NEGATIVE_INFINITY

  for (let drawableIndex = 0; drawableIndex < cubismModel.getDrawableCount(); drawableIndex += 1) {
    const vertices = cubismModel.getDrawableVertices(drawableIndex)

    for (let i = 0; i < vertices.length; i += 2) {
      const x = vertices[i]
      const y = vertices[i + 1]

      minX = Math.min(minX, x)
      minY = Math.min(minY, y)
      maxX = Math.max(maxX, x)
      maxY = Math.max(maxY, y)
    }
  }

  if (!Number.isFinite(minX) || !Number.isFinite(minY) || !Number.isFinite(maxX) || !Number.isFinite(maxY)) {
    return null
  }

  return { minX, minY, maxX, maxY }
}
