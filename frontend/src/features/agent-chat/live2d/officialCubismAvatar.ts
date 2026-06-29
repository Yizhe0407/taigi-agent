declare global {
  interface Window {
    Live2DCubismCore?: unknown
  }
}

const CUBISM_CORE_SRC = "/vendor/live2dcubismcore.min.js"
const CUBISM_SHADER_PATH = "/vendor/live2d/shaders/webgl/"
const AVATAR_VISIBLE_HEIGHT = 2.1
const AVATAR_TOP_Y = 0.85

let coreScriptPromise: Promise<void> | null = null
let frameworkPromise: Promise<void> | null = null
let CubismFramework: any = null
let CubismModelSettingJson: any = null
let CubismMatrix44: any = null
let CubismUserModel: any = null

export class OfficialCubismAvatar {
  private readonly host: HTMLElement
  private readonly modelSrc: string
  private readonly canvas: HTMLCanvasElement
  private gl: WebGLRenderingContext | WebGL2RenderingContext | null = null
  private model: any = null
  private renderer: any = null
  private textures: WebGLTexture[] = []
  private frameId = 0
  private disposed = false
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

  constructor(host: HTMLElement, modelSrc: string) {
    this.host = host
    this.modelSrc = modelSrc
    this.canvas = document.createElement("canvas")
    this.canvas.className = "absolute inset-0 h-full w-full"
    this.host.appendChild(this.canvas)
  }

  async load() {
    await ensureCubismFramework()
    this.resize()

    this.gl = createWebGlContext(this.canvas)
    if (!this.gl) throw new Error("Unable to create a WebGL context for Live2D")

    const modelUrl = new URL(this.modelSrc, window.location.href)
    const modelRootUrl = new URL(".", modelUrl)
    const settingBuffer = await fetchArrayBuffer(modelUrl)
    const setting = new CubismModelSettingJson(settingBuffer, settingBuffer.byteLength)

    const mocUrl = new URL(setting.getModelFileName(), modelRootUrl)
    const mocBuffer = await fetchArrayBuffer(mocUrl)

    this.model = new CubismUserModel()
    this.model.loadModel(mocBuffer, false)
    this.applyModelLayout(setting)

    this.model.createRenderer(this.canvas.width, this.canvas.height, 1)
    this.renderer = this.model.getRenderer()
    this.renderer.startUp(this.gl)
    this.renderer.setIsPremultipliedAlpha(true)

    await this.loadTextures(setting, modelRootUrl)
    this.start()
  }

  setMouthAmplitude(value: number) {
    this.mouthTarget = value
  }

  resize() {
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

  dispose() {
    this.disposed = true
    cancelAnimationFrame(this.frameId)

    for (const texture of this.textures) {
      this.gl?.deleteTexture(texture)
    }

    this.textures = []
    this.model?.release()
    this.model = null
    this.renderer = null
    this.canvas.remove()
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

  private async loadTextures(setting: any, modelRootUrl: URL) {
    const textureDirectory = setting.getTextureDirectory()

    for (let index = 0; index < setting.getTextureCount(); index += 1) {
      const textureFileName = setting.getTextureFileName(index)
      const texturePath = textureFileName.includes("/") || !textureDirectory
        ? textureFileName
        : `${textureDirectory}/${textureFileName}`
      const image = await loadImage(new URL(texturePath, modelRootUrl))
      const texture = createTexture(this.gl!, image)

      this.textures.push(texture)
      this.renderer.bindTexture(index, texture)
    }
  }

  private start() {
    const tick = () => {
      if (this.disposed) return

      this.render()
      this.frameId = requestAnimationFrame(tick)
    }

    tick()
  }

  private render() {
    if (!this.gl || !this.model || !this.renderer) return

    this.resize()
    this.updateParameters()

    const cubismModel = this.model.getModel()
    cubismModel.update()

    this.gl.clearColor(0, 0, 0, 0)
    this.gl.clear(this.gl.COLOR_BUFFER_BIT)

    const projection = new CubismMatrix44()
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

    // Random-interval blink with smooth eyelid curve
    const blink = this.computeBlink(now)
    this.setParameter("ParamEyeLOpen", blink)
    this.setParameter("ParamEyeROpen", blink)

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
      id = CubismFramework.getIdManager().getId(name)
      this.parameterIds.set(name, id)
    }

    cubismModel.setParameterValueById(id, value)
  }
}

async function ensureCubismFramework() {
  if (!frameworkPromise) {
    frameworkPromise = ensureCubismCore().then(async () => {
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

      CubismFramework = frameworkModule.CubismFramework
      CubismModelSettingJson = settingModule.CubismModelSettingJson
      CubismMatrix44 = matrixModule.CubismMatrix44
      CubismUserModel = userModelModule.CubismUserModel

      if (!CubismFramework.isStarted()) {
        const option = new frameworkModule.Option()
        option.logFunction = () => undefined
        option.loggingLevel = frameworkModule.LogLevel.LogLevel_Off
        CubismFramework.startUp(option)
      }

      if (!CubismFramework.isInitialized()) {
        CubismFramework.initialize()
      }
    })
  }

  await frameworkPromise
}

async function ensureCubismCore() {
  if (window.Live2DCubismCore) return

  if (!coreScriptPromise) {
    coreScriptPromise = new Promise<void>((resolve, reject) => {
      const script = document.createElement("script")
      script.src = CUBISM_CORE_SRC
      script.async = true
      script.onload = () => resolve()
      script.onerror = () => reject(new Error("Unable to load Live2D Cubism Core"))
      document.head.appendChild(script)
    })
  }

  await coreScriptPromise
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

async function fetchArrayBuffer(url: URL) {
  const response = await fetch(url)
  if (!response.ok) throw new Error(`Unable to load ${url.pathname}`)
  return response.arrayBuffer()
}

async function loadImage(url: URL) {
  const image = new Image()
  image.decoding = "async"

  await new Promise<void>((resolve, reject) => {
    image.onload = () => resolve()
    image.onerror = () => reject(new Error(`Unable to load ${url.pathname}`))
    image.src = url.href
  })

  return image
}

function createTexture(
  gl: WebGLRenderingContext | WebGL2RenderingContext,
  image: HTMLImageElement,
) {
  const texture = gl.createTexture()
  if (!texture) throw new Error("Unable to create a WebGL texture")

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
