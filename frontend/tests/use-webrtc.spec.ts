import { effectScope } from "vue"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { useWebRTC } from "@/features/agent-chat/composables/useWebRTC"
import { settleRetainedAsynchronousTeardowns } from "@/lib/component-lifecycle"
import { reportClientEvent } from "@/lib/report-client-event"

vi.mock("@/lib/report-client-event", () => ({
  reportClientEvent: vi.fn(),
}))

type ConnectedHarnessOptions = {
  contextState?: AudioContextState
  resumeError?: Error
  resumePromise?: Promise<void>
  closePromise?: Promise<void>
  iceGatheringState?: RTCIceGatheringState
  sourceConnectError?: Error
  createAnalyserError?: Error
  dataChannelSendError?: Error
  dataChannelState?: RTCDataChannelState
}

function installConnectedHarness(options: ConnectedHarnessOptions = {}) {
  const stopLocalTrack = vi.fn()
  const closeContext = vi.fn(() => options.closePromise ?? Promise.resolve())
  const disconnectSource = vi.fn()
  const disconnectAnalyser = vi.fn()
  const source = {
    connect: vi.fn(() => {
      if (options.sourceConnectError) throw options.sourceConnectError
    }),
    disconnect: disconnectSource,
  }
  const analyser = {
    fftSize: 0,
    smoothingTimeConstant: 0,
    minDecibels: 0,
    maxDecibels: 0,
    frequencyBinCount: 32,
    connect: vi.fn(),
    disconnect: disconnectAnalyser,
    getByteFrequencyData: vi.fn(),
  }
  const createMediaStreamSource = vi.fn(() => source)
  const createAnalyser = vi.fn(() => {
    if (options.createAnalyserError) throw options.createAnalyserError
    return analyser
  })

  class FakeAudioContext {
    static instances: FakeAudioContext[] = []

    state: AudioContextState = options.contextState ?? "running"
    sampleRate = 48_000
    destination = {}
    close = vi.fn(async () => {
      await closeContext()
      this.state = "closed"
    })
    createMediaStreamSource = createMediaStreamSource
    createAnalyser = createAnalyser
    resume = vi.fn(async () => {
      if (options.resumePromise) await options.resumePromise
      if (options.resumeError) throw options.resumeError
      if (this.state !== "closed") this.state = "running"
    })

    constructor() {
      FakeAudioContext.instances.push(this)
    }
  }

  const localStream = {
    getTracks: () => [{ stop: stopLocalTrack }],
  }
  const getUserMedia = vi.fn(async () => localStream)
  Object.defineProperty(navigator, "mediaDevices", {
    configurable: true,
    value: { getUserMedia },
  })

  class FakeDataChannel {
    readyState: RTCDataChannelState = options.dataChannelState ?? "open"
    onopen: (() => void) | null = null
    onmessage: ((event: MessageEvent) => void) | null = null
    send = vi.fn(() => {
      if (options.dataChannelSendError) throw options.dataChannelSendError
    })
    close = vi.fn(() => {
      this.readyState = "closed"
    })
  }

  class FakePeerConnection {
    static instances: FakePeerConnection[] = []

    readonly channel = new FakeDataChannel()
    readonly sender = {}
    iceGatheringState: RTCIceGatheringState = options.iceGatheringState ?? "complete"
    iceConnectionState: RTCIceConnectionState = "new"
    localDescription: RTCSessionDescriptionInit | null = null
    ontrack: ((event: RTCTrackEvent) => void) | null = null
    oniceconnectionstatechange: (() => void) | null = null
    close = vi.fn()
    addTrack = vi.fn(() => this.sender)
    getTransceivers = vi.fn(() => [{ sender: this.sender, direction: "sendonly" }])
    createDataChannel = vi.fn(() => this.channel)
    createOffer = vi.fn(async () => ({ sdp: "offer", type: "offer" as RTCSdpType }))
    setLocalDescription = vi.fn(async (description: RTCSessionDescriptionInit) => {
      this.localDescription = description
    })
    setRemoteDescription = vi.fn(async () => {})
    addEventListener = vi.fn()
    removeEventListener = vi.fn()

    constructor() {
      FakePeerConnection.instances.push(this)
    }
  }

  vi.stubGlobal("AudioContext", FakeAudioContext)
  vi.stubGlobal("RTCPeerConnection", FakePeerConnection)
  vi.stubGlobal("requestAnimationFrame", vi.fn(() => 1))
  vi.stubGlobal("cancelAnimationFrame", vi.fn())
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    if (url.includes("/api/voice/ice-servers")) {
      return new Response(
        JSON.stringify({ iceServers: [] }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      )
    }
    return new Response(
      JSON.stringify({ sdp: "answer", type: "answer", pc_id: "peer-id" }),
      { status: 200, headers: { "Content-Type": "application/json" } },
    )
  })
  vi.stubGlobal("fetch", fetchMock)

  return {
    FakeAudioContext,
    FakePeerConnection,
    stopLocalTrack,
    getUserMedia,
    closeContext,
    fetchMock,
    createMediaStreamSource,
    createAnalyser,
    source,
    analyser,
    disconnectSource,
    disconnectAnalyser,
  }
}

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}

async function flushMicrotasks(rounds = 12) {
  for (let index = 0; index < rounds; index++) await Promise.resolve()
}

function makeRemoteStream() {
  const stopTrack = vi.fn()
  const track = { stop: stopTrack }
  const stream = new MediaStream()
  Object.defineProperty(stream, "getTracks", {
    configurable: true,
    value: vi.fn(() => [track as unknown as MediaStreamTrack]),
  })
  return { stream, track, stopTrack }
}

function nestedErrorMessages(error: unknown): string[] {
  if (error instanceof AggregateError) {
    return [error.message, ...error.errors.flatMap(nestedErrorMessages)]
  }
  return [error instanceof Error ? error.message : String(error)]
}

async function flushOwnedOperations() {
  for (let index = 0; index < 12; index += 1) await Promise.resolve()
}

describe("useWebRTC", () => {
  const originalMediaDevices = Object.getOwnPropertyDescriptor(navigator, "mediaDevices")

  beforeEach(() => {
    vi.mocked(reportClientEvent).mockClear()
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
    document.querySelectorAll("audio").forEach(audio => audio.remove())
    if (originalMediaDevices) {
      Object.defineProperty(navigator, "mediaDevices", originalMediaDevices)
    } else {
      Reflect.deleteProperty(navigator, "mediaDevices")
    }
  })

  it("releases acquired microphone and AudioContext when peer construction throws", async () => {
    const stopTrack = vi.fn()
    const closeContext = vi.fn(async () => {})
    const getUserMedia = vi.fn(async () => ({
      getTracks: () => [{ stop: stopTrack }],
    }))
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: { getUserMedia },
    })

    class FakeAudioContext {
      state = "running"
      close = closeContext
    }

    class BrokenPeerConnection {
      constructor() {
        throw new Error("peer constructor failed")
      }
    }

    vi.stubGlobal("AudioContext", FakeAudioContext)
    vi.stubGlobal("RTCPeerConnection", BrokenPeerConnection)
    vi.stubGlobal("fetch", vi.fn(async () => new Response(
      JSON.stringify({ iceServers: [] }),
      { status: 200, headers: { "Content-Type": "application/json" } },
    )))

    const scope = effectScope()
    const webrtc = scope.run(() => useWebRTC({
      onTranscript: vi.fn(),
      onReply: vi.fn(),
    }))
    expect(webrtc).toBeDefined()

    await expect(webrtc!.connect("019d0000-0000-7000-8000-000000000001")).resolves.toBeUndefined()

    expect(getUserMedia).toHaveBeenCalledOnce()
    expect(stopTrack).toHaveBeenCalledOnce()
    expect(closeContext).toHaveBeenCalledOnce()
    expect(webrtc!.state.value).toBe("error")

    scope.stop()
    await flushOwnedOperations()
    expect(stopTrack).toHaveBeenCalledOnce()
    expect(closeContext).toHaveBeenCalledOnce()
  })

  it("reports remote audio playback rejection and releases the connection once", async () => {
    const harness = installConnectedHarness()
    const play = vi.spyOn(HTMLMediaElement.prototype, "play")
      .mockRejectedValue(new Error("autoplay blocked"))
    vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => {})
    vi.spyOn(HTMLMediaElement.prototype, "load").mockImplementation(() => {})

    const scope = effectScope()
    const webrtc = scope.run(() => useWebRTC({
      onTranscript: vi.fn(),
      onReply: vi.fn(),
    }))
    expect(webrtc).toBeDefined()
    await webrtc!.connect("019d0000-0000-7000-8000-000000000002")

    const peer = harness.FakePeerConnection.instances[0]!
    const remote = makeRemoteStream()
    peer.ontrack?.({ track: remote.track, streams: [remote.stream] } as unknown as RTCTrackEvent)

    await vi.waitFor(() => {
      expect(reportClientEvent).toHaveBeenCalledWith(
        "webrtc_remote_audio_error",
        "autoplay blocked",
      )
    })

    expect(play).toHaveBeenCalledOnce()
    expect(webrtc!.state.value).toBe("error")
    expect(peer.close).toHaveBeenCalledOnce()
    expect(peer.channel.close).toHaveBeenCalledOnce()
    expect(harness.stopLocalTrack).toHaveBeenCalledOnce()
    expect(remote.stopTrack).toHaveBeenCalledOnce()
    expect(harness.closeContext).toHaveBeenCalledOnce()

    scope.stop()
    await flushOwnedOperations()
    expect(peer.close).toHaveBeenCalledOnce()
    expect(peer.channel.close).toHaveBeenCalledOnce()
    expect(harness.stopLocalTrack).toHaveBeenCalledOnce()
    expect(remote.stopTrack).toHaveBeenCalledOnce()
    expect(harness.closeContext).toHaveBeenCalledOnce()
  })

  it("reports AudioContext resume rejection and owns one retry listener", async () => {
    const harness = installConnectedHarness({
      contextState: "suspended",
      resumeError: new Error("resume blocked"),
    })
    vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue()
    vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => {})
    vi.spyOn(HTMLMediaElement.prototype, "load").mockImplementation(() => {})
    const addEventListener = vi.spyOn(document, "addEventListener")
    const removeEventListener = vi.spyOn(document, "removeEventListener")

    const scope = effectScope()
    const webrtc = scope.run(() => useWebRTC({
      onTranscript: vi.fn(),
      onReply: vi.fn(),
    }))
    expect(webrtc).toBeDefined()
    await webrtc!.connect("019d0000-0000-7000-8000-000000000003")

    const peer = harness.FakePeerConnection.instances[0]!
    const remote = makeRemoteStream()
    peer.ontrack?.({ track: remote.track, streams: [remote.stream] } as unknown as RTCTrackEvent)

    await vi.waitFor(() => {
      expect(reportClientEvent).toHaveBeenCalledWith(
        "webrtc_audio_resume_error",
        "resume blocked",
      )
    })
    expect(
      addEventListener.mock.calls.filter(([type]) => type === "pointerdown"),
    ).toHaveLength(1)

    scope.stop()
    await flushOwnedOperations()
    expect(
      removeEventListener.mock.calls.filter(([type]) => type === "pointerdown"),
    ).toHaveLength(1)
    expect(harness.disconnectSource).toHaveBeenCalledOnce()
    expect(harness.disconnectAnalyser).toHaveBeenCalledOnce()
  })

  it("transfers remote stream ownership and ignores a stale play continuation", async () => {
    const harness = installConnectedHarness()
    let resolveFirstPlay!: () => void
    const firstPlay = new Promise<void>(resolve => {
      resolveFirstPlay = resolve
    })
    const play = vi.spyOn(HTMLMediaElement.prototype, "play")
      .mockImplementationOnce(() => firstPlay)
      .mockResolvedValueOnce()
    vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => {})
    vi.spyOn(HTMLMediaElement.prototype, "load").mockImplementation(() => {})

    const scope = effectScope()
    const webrtc = scope.run(() => useWebRTC({
      onTranscript: vi.fn(),
      onReply: vi.fn(),
    }))
    expect(webrtc).toBeDefined()
    await webrtc!.connect("019d0000-0000-7000-8000-000000000004")

    const peer = harness.FakePeerConnection.instances[0]!
    const first = makeRemoteStream()
    const second = makeRemoteStream()

    peer.ontrack?.({ track: first.track, streams: [first.stream] } as unknown as RTCTrackEvent)
    await vi.waitFor(() => expect(play).toHaveBeenCalledOnce())

    peer.ontrack?.({ track: second.track, streams: [second.stream] } as unknown as RTCTrackEvent)
    await vi.waitFor(() => {
      expect(first.stopTrack).toHaveBeenCalledOnce()
      expect(play).toHaveBeenCalledTimes(2)
    })
    expect(second.stopTrack).not.toHaveBeenCalled()

    await vi.waitFor(() => {
      expect(harness.createMediaStreamSource).toHaveBeenCalledOnce()
      expect(harness.createMediaStreamSource).toHaveBeenCalledWith(second.stream)
      expect(peer.channel.send).toHaveBeenCalledOnce()
    })

    resolveFirstPlay()
    await Promise.resolve()
    await Promise.resolve()
    expect(harness.createMediaStreamSource).toHaveBeenCalledOnce()
    expect(peer.channel.send).toHaveBeenCalledOnce()

    scope.stop()
    await flushOwnedOperations()
    expect(first.stopTrack).toHaveBeenCalledOnce()
    expect(second.stopTrack).toHaveBeenCalledOnce()
    expect(harness.disconnectSource).toHaveBeenCalledOnce()
    expect(harness.disconnectAnalyser).toHaveBeenCalledOnce()
  })

  it("disconnects exactly once while remote audio play is still pending", async () => {
    const harness = installConnectedHarness()
    const pendingPlay = deferred<void>()
    const play = vi.spyOn(HTMLMediaElement.prototype, "play")
      .mockImplementation(() => pendingPlay.promise)
    vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => {})
    vi.spyOn(HTMLMediaElement.prototype, "load").mockImplementation(() => {})

    const scope = effectScope()
    const webrtc = scope.run(() => useWebRTC({
      onTranscript: vi.fn(),
      onReply: vi.fn(),
    }))
    expect(webrtc).toBeDefined()
    await webrtc!.connect("019d0000-0000-7000-8000-000000000005")

    const peer = harness.FakePeerConnection.instances[0]!
    const remote = makeRemoteStream()
    peer.ontrack?.({ track: remote.track, streams: [remote.stream] } as unknown as RTCTrackEvent)
    await vi.waitFor(() => expect(play).toHaveBeenCalledOnce())

    let disconnected = false
    const disconnecting = webrtc!.disconnect().then(() => {
      disconnected = true
    })
    expect(webrtc!.state.value).toBe("disconnected")
    await vi.waitFor(() => {
      expect(peer.close).toHaveBeenCalledOnce()
      expect(peer.channel.close).toHaveBeenCalledOnce()
      expect(harness.stopLocalTrack).toHaveBeenCalledOnce()
      expect(remote.stopTrack).toHaveBeenCalledOnce()
      expect(harness.closeContext).toHaveBeenCalledOnce()
    })
    await flushMicrotasks()
    expect(disconnected).toBe(false)

    pendingPlay.resolve()
    await expect(disconnecting).resolves.toBeUndefined()
    expect(peer.channel.send).not.toHaveBeenCalled()
    expect(harness.createMediaStreamSource).not.toHaveBeenCalled()

    scope.stop()
    await flushOwnedOperations()
    expect(peer.close).toHaveBeenCalledOnce()
    expect(peer.channel.close).toHaveBeenCalledOnce()
    expect(remote.stopTrack).toHaveBeenCalledOnce()
    expect(harness.closeContext).toHaveBeenCalledOnce()
  })

  it("owns one exact remote audio attachment cleanup", async () => {
    const harness = installConnectedHarness()
    vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue()
    vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => {})
    vi.spyOn(HTMLMediaElement.prototype, "load").mockImplementation(() => {})

    const originalSrcObject = Object.getOwnPropertyDescriptor(
      HTMLMediaElement.prototype,
      "srcObject",
    )
    const attached = new WeakMap<HTMLMediaElement, MediaProvider | null>()
    const setSrcObject = vi.fn(function (
      this: HTMLMediaElement,
      value: MediaProvider | null,
    ) {
      attached.set(this, value)
    })
    Object.defineProperty(HTMLMediaElement.prototype, "srcObject", {
      configurable: true,
      get(this: HTMLMediaElement) { return attached.get(this) ?? null },
      set: setSrcObject,
    })

    const scope = effectScope()
    try {
      const webrtc = scope.run(() => useWebRTC({
        onTranscript: vi.fn(),
        onReply: vi.fn(),
      }))
      expect(webrtc).toBeDefined()
      await webrtc!.connect("019d0000-0000-7000-8000-000000000039")

      const peer = harness.FakePeerConnection.instances[0]!
      const remote = makeRemoteStream()
      peer.ontrack?.({ track: remote.track, streams: [remote.stream] } as unknown as RTCTrackEvent)
      await vi.waitFor(() => expect(HTMLMediaElement.prototype.play).toHaveBeenCalledOnce())

      await webrtc!.disconnect()
      const nullAssignments = setSrcObject.mock.calls.filter(([value]) => value === null)
      expect(nullAssignments).toHaveLength(1)
      expect(remote.stopTrack).toHaveBeenCalledOnce()
    } finally {
      scope.stop()
      await flushOwnedOperations()
      if (originalSrcObject) {
        Object.defineProperty(HTMLMediaElement.prototype, "srcObject", originalSrcObject)
      } else {
        Reflect.deleteProperty(HTMLMediaElement.prototype, "srcObject")
      }
    }
  })

  it("does not re-arm audio resume listeners after disconnecting a pending resume", async () => {
    const pendingResume = deferred<void>()
    const harness = installConnectedHarness({
      contextState: "suspended",
      resumePromise: pendingResume.promise,
    })
    vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue()
    vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => {})
    vi.spyOn(HTMLMediaElement.prototype, "load").mockImplementation(() => {})
    const addEventListener = vi.spyOn(document, "addEventListener")
    const removeEventListener = vi.spyOn(document, "removeEventListener")

    const scope = effectScope()
    const webrtc = scope.run(() => useWebRTC({
      onTranscript: vi.fn(),
      onReply: vi.fn(),
    }))
    expect(webrtc).toBeDefined()
    await webrtc!.connect("019d0000-0000-7000-8000-000000000006")

    const peer = harness.FakePeerConnection.instances[0]!
    const remote = makeRemoteStream()
    peer.ontrack?.({ track: remote.track, streams: [remote.stream] } as unknown as RTCTrackEvent)
    await vi.waitFor(() => expect(harness.createMediaStreamSource).toHaveBeenCalledOnce())
    expect(
      addEventListener.mock.calls.filter(([type]) => type === "pointerdown"),
    ).toHaveLength(1)

    const disconnecting = webrtc!.disconnect()
    await vi.waitFor(() => {
      expect(
        removeEventListener.mock.calls.filter(([type]) => type === "pointerdown"),
      ).toHaveLength(1)
      expect(harness.disconnectSource).toHaveBeenCalledOnce()
      expect(harness.disconnectAnalyser).toHaveBeenCalledOnce()
    })

    pendingResume.resolve()
    await expect(disconnecting).resolves.toBeUndefined()
    expect(
      addEventListener.mock.calls.filter(([type]) => type === "pointerdown"),
    ).toHaveLength(1)
    expect(
      removeEventListener.mock.calls.filter(([type]) => type === "pointerdown"),
    ).toHaveLength(1)
    expect(reportClientEvent).not.toHaveBeenCalledWith(
      "webrtc_audio_resume_error",
      expect.any(String),
    )

    scope.stop()
    await flushOwnedOperations()
  })

  it("aborts pending ICE gathering and removes its timeout and listener", async () => {
    vi.useFakeTimers()
    const harness = installConnectedHarness({ iceGatheringState: "gathering" })
    const scope = effectScope()
    const webrtc = scope.run(() => useWebRTC({
      onTranscript: vi.fn(),
      onReply: vi.fn(),
    }))
    expect(webrtc).toBeDefined()

    const connecting = webrtc!.connect("019d0000-0000-7000-8000-000000000007")
    await flushMicrotasks()
    const peer = harness.FakePeerConnection.instances[0]!
    expect(peer.addEventListener).toHaveBeenCalledWith(
      "icegatheringstatechange",
      expect.any(Function),
    )
    expect(vi.getTimerCount()).toBe(1)

    const disconnecting = webrtc!.disconnect()
    await Promise.all([connecting, disconnecting])
    expect(peer.removeEventListener).toHaveBeenCalledWith(
      "icegatheringstatechange",
      expect.any(Function),
    )
    expect(vi.getTimerCount()).toBe(0)
    expect(harness.fetchMock).toHaveBeenCalledOnce()
    expect(peer.close).toHaveBeenCalledOnce()
    expect(peer.channel.close).toHaveBeenCalledOnce()
    expect(harness.stopLocalTrack).toHaveBeenCalledOnce()
    expect(harness.closeContext).toHaveBeenCalledOnce()

    scope.stop()
    await flushOwnedOperations()
  })

  it("rolls back the exact ICE deadline when teardown re-enters timer acquisition", async () => {
    const harness = installConnectedHarness({ iceGatheringState: "gathering" })
    const exactTimerId = 731
    const clearTimeoutSpy = vi.spyOn(window, "clearTimeout")
    const passthroughSetTimeout = window.setTimeout.bind(window)
    let disconnecting: Promise<void> | null = null
    let webrtc: ReturnType<typeof useWebRTC> | undefined
    vi.spyOn(window, "setTimeout").mockImplementation((handler, timeout, ...args) => {
      if (timeout === 10_000) {
        disconnecting = webrtc!.disconnect()
        return exactTimerId
      }
      return passthroughSetTimeout(handler, timeout, ...args)
    })

    const scope = effectScope()
    webrtc = scope.run(() => useWebRTC({
      onTranscript: vi.fn(),
      onReply: vi.fn(),
    }))
    expect(webrtc).toBeDefined()

    const connecting = webrtc!.connect("019d0000-0000-7000-8000-000000000026")
    await vi.waitFor(() => expect(disconnecting).not.toBeNull())
    await Promise.all([connecting, disconnecting!])

    expect(clearTimeoutSpy).toHaveBeenCalledWith(exactTimerId)
    expect(harness.FakePeerConnection.instances[0]!.addEventListener).not.toHaveBeenCalled()
    expect(harness.FakePeerConnection.instances[0]!.close).toHaveBeenCalledOnce()
    expect(reportClientEvent).not.toHaveBeenCalledWith(
      "webrtc_cleanup_error",
      expect.stringContaining("ICE gathering deadline"),
    )

    scope.stop()
    await flushOwnedOperations()
  })

  it("publishes the connection owner before queued work so scope disposal prevents microphone acquisition", async () => {
    const harness = installConnectedHarness()
    const scope = effectScope()
    const webrtc = scope.run(() => useWebRTC({
      onTranscript: vi.fn(),
      onReply: vi.fn(),
    }))
    expect(webrtc).toBeDefined()

    const connecting = webrtc!.connect("019d0000-0000-7000-8000-000000000020")
    // connect() publishes its owner and physical operation synchronously, while
    // runConnect() is still queued behind the current stack.
    scope.stop()

    await expect(connecting).resolves.toBeUndefined()
    await flushOwnedOperations()
    expect(harness.getUserMedia).not.toHaveBeenCalled()
    expect(harness.FakeAudioContext.instances).toHaveLength(0)
    expect(harness.FakePeerConnection.instances).toHaveLength(0)
    expect(harness.fetchMock).not.toHaveBeenCalled()
  })

  it("contains a throwing ICE-timeout reporter inside the connection lifecycle", async () => {
    vi.useFakeTimers()
    const reporterError = new Error("ICE timeout reporter failed")
    vi.mocked(reportClientEvent).mockImplementation((type) => {
      if (type === "webrtc_ice_gathering_timeout") throw reporterError
    })
    const harness = installConnectedHarness({ iceGatheringState: "gathering" })
    const scope = effectScope()
    const webrtc = scope.run(() => useWebRTC({
      onTranscript: vi.fn(),
      onReply: vi.fn(),
    }))
    expect(webrtc).toBeDefined()

    const connecting = webrtc!.connect("019d0000-0000-7000-8000-000000000021")
    await flushMicrotasks()
    await vi.advanceTimersByTimeAsync(10_000)
    await expect(connecting).resolves.toBeUndefined()

    expect(reportClientEvent).toHaveBeenCalledWith(
      "webrtc_ice_gathering_timeout",
      "ICE gathering state: gathering",
    )
    expect(reportClientEvent).toHaveBeenCalledWith(
      "webrtc_connect_error",
      reporterError.message,
    )
    const peer = harness.FakePeerConnection.instances[0]!
    expect(peer.close).toHaveBeenCalledOnce()
    expect(peer.channel.close).toHaveBeenCalledOnce()
    expect(harness.stopLocalTrack).toHaveBeenCalledOnce()
    expect(harness.closeContext).toHaveBeenCalledOnce()
    expect(vi.getTimerCount()).toBe(0)

    scope.stop()
    await flushOwnedOperations()
  })

  it("retries a failed ICE deadline cancellation only on the next public attempt", async () => {
    vi.useFakeTimers()
    const harness = installConnectedHarness({ iceGatheringState: "gathering" })
    const setTimeoutSpy = vi.spyOn(window, "setTimeout")
    const cleanupError = new Error("ICE deadline clear failed")
    const clearTimeoutSpy = vi.spyOn(window, "clearTimeout")
      .mockImplementationOnce(() => { throw cleanupError })
    const scope = effectScope()
    const webrtc = scope.run(() => useWebRTC({
      onTranscript: vi.fn(),
      onReply: vi.fn(),
    }))
    expect(webrtc).toBeDefined()

    const connecting = webrtc!.connect("019d0000-0000-7000-8000-000000000022")
    await flushMicrotasks()
    const timeoutCall = setTimeoutSpy.mock.calls.find(([, delay]) => delay === 10_000)
    const timeoutCallback = timeoutCall?.[0]
    expect(typeof timeoutCallback).toBe("function")

    const disconnecting = webrtc!.disconnect()
    await expect(disconnecting).rejects.toThrow("WebRTC teardown failed")
    await expect(connecting).rejects.toThrow("WebRTC teardown failed")
    expect(clearTimeoutSpy).toHaveBeenCalledOnce()

    const timeoutReports = () => vi.mocked(reportClientEvent).mock.calls.filter(
      ([type]) => type === "webrtc_ice_gathering_timeout",
    )
    ;(timeoutCallback as () => void)()
    expect(timeoutReports()).toHaveLength(0)
    expect(clearTimeoutSpy).toHaveBeenCalledOnce()

    await expect(webrtc!.disconnect()).resolves.toBeUndefined()
    expect(clearTimeoutSpy).toHaveBeenCalledTimes(2)
    expect(clearTimeoutSpy.mock.calls[1]![0]).toBe(clearTimeoutSpy.mock.calls[0]![0])
    expect(timeoutReports()).toHaveLength(0)
    expect(harness.FakePeerConnection.instances[0]!.close).toHaveBeenCalledOnce()
    expect(harness.stopLocalTrack).toHaveBeenCalledOnce()
    expect(harness.closeContext).toHaveBeenCalledOnce()

    scope.stop()
    await flushOwnedOperations()
  })

  it("turns an ICE state callback getter failure into an owned terminal failure", async () => {
    const stateError = new Error("ICE state getter failed")
    const harness = installConnectedHarness({ iceGatheringState: "gathering" })
    const scope = effectScope()
    const webrtc = scope.run(() => useWebRTC({
      onTranscript: vi.fn(),
      onReply: vi.fn(),
    }))
    expect(webrtc).toBeDefined()

    const connecting = webrtc!.connect("019d0000-0000-7000-8000-000000000023")
    await flushMicrotasks()
    const peer = harness.FakePeerConnection.instances[0]!
    const stateHandler = peer.addEventListener.mock.calls.find(
      ([type]) => type === "icegatheringstatechange",
    )?.[1] as EventListener | undefined
    expect(stateHandler).toBeDefined()
    Object.defineProperty(peer, "iceGatheringState", {
      configurable: true,
      get: () => { throw stateError },
    })

    stateHandler?.(new Event("icegatheringstatechange"))
    await expect(connecting).resolves.toBeUndefined()

    expect(reportClientEvent).toHaveBeenCalledWith(
      "webrtc_connect_error",
      stateError.message,
    )
    expect(peer.removeEventListener).toHaveBeenCalledWith(
      "icegatheringstatechange",
      stateHandler,
    )
    expect(peer.close).toHaveBeenCalledOnce()
    expect(harness.stopLocalTrack).toHaveBeenCalledOnce()
    expect(harness.closeContext).toHaveBeenCalledOnce()

    scope.stop()
    await flushOwnedOperations()
  })

  it("preserves both an ICE callback failure and its exact cleanup failure", async () => {
    const stateError = new Error("ICE callback state getter failed")
    const cleanupError = new Error("ICE listener removal failed")
    const harness = installConnectedHarness({ iceGatheringState: "gathering" })
    const scope = effectScope()
    const webrtc = scope.run(() => useWebRTC({
      onTranscript: vi.fn(),
      onReply: vi.fn(),
    }))
    expect(webrtc).toBeDefined()

    const connecting = webrtc!.connect("019d0000-0000-7000-8000-000000000037")
    await flushMicrotasks()
    const peer = harness.FakePeerConnection.instances[0]!
    const stateHandler = peer.addEventListener.mock.calls.find(
      ([type]) => type === "icegatheringstatechange",
    )?.[1] as EventListener | undefined
    expect(stateHandler).toBeDefined()
    peer.removeEventListener.mockImplementationOnce(() => {
      throw cleanupError
    })
    Object.defineProperty(peer, "iceGatheringState", {
      configurable: true,
      get: () => { throw stateError },
    })

    stateHandler?.(new Event("icegatheringstatechange"))
    let failure: unknown
    try {
      await connecting
    } catch (error) {
      failure = error
    }

    expect(nestedErrorMessages(failure)).toEqual(expect.arrayContaining([
      "ICE gathering operation and cleanup failed",
      stateError.message,
      expect.stringContaining(cleanupError.message),
    ]))
    expect(peer.removeEventListener).toHaveBeenCalledTimes(1)

    // The failed exact listener claim is not replayed recursively by connection
    // teardown. A distinct public attempt retries only that retained claim.
    await expect(webrtc!.disconnect()).resolves.toBeUndefined()
    expect(peer.removeEventListener).toHaveBeenCalledTimes(2)
    expect(peer.close).toHaveBeenCalledOnce()
    expect(harness.stopLocalTrack).toHaveBeenCalledOnce()
    expect(harness.closeContext).toHaveBeenCalledOnce()

    scope.stop()
    await flushOwnedOperations()
  })

  it("turns an ICE connection-state getter failure into terminal teardown", async () => {
    const stateError = new Error("ICE connection state getter failed")
    const harness = installConnectedHarness()
    const scope = effectScope()
    const webrtc = scope.run(() => useWebRTC({
      onTranscript: vi.fn(),
      onReply: vi.fn(),
    }))
    expect(webrtc).toBeDefined()
    await webrtc!.connect("019d0000-0000-7000-8000-000000000024")

    const peer = harness.FakePeerConnection.instances[0]!
    const stateHandler = peer.oniceconnectionstatechange
    expect(stateHandler).not.toBeNull()
    Object.defineProperty(peer, "iceConnectionState", {
      configurable: true,
      get: () => { throw stateError },
    })

    stateHandler?.()
    await vi.waitFor(() => {
      expect(reportClientEvent).toHaveBeenCalledWith(
        "webrtc_ice_state_error",
        stateError.message,
      )
      expect(peer.close).toHaveBeenCalledOnce()
    })
    expect(webrtc!.state.value).toBe("error")
    expect(harness.stopLocalTrack).toHaveBeenCalledOnce()
    expect(harness.closeContext).toHaveBeenCalledOnce()

    const reportCount = vi.mocked(reportClientEvent).mock.calls.length
    stateHandler?.()
    await flushOwnedOperations()
    expect(vi.mocked(reportClientEvent).mock.calls).toHaveLength(reportCount)
    expect(peer.close).toHaveBeenCalledOnce()

    scope.stop()
    await flushOwnedOperations()
  })

  it("consumes a failing amplitude frame and never rearms it during terminal teardown", async () => {
    const analyserError = new Error("frequency sampling failed")
    const harness = installConnectedHarness()
    const frames = new Map<number, FrameRequestCallback>()
    let nextFrameId = 1
    const requestFrame = vi.fn((callback: FrameRequestCallback) => {
      const frameId = nextFrameId++
      frames.set(frameId, callback)
      return frameId
    })
    const cancelFrame = vi.fn((frameId: number) => {
      frames.delete(frameId)
    })
    vi.stubGlobal("requestAnimationFrame", requestFrame)
    vi.stubGlobal("cancelAnimationFrame", cancelFrame)
    vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue()
    vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => {})
    vi.spyOn(HTMLMediaElement.prototype, "load").mockImplementation(() => {})

    const scope = effectScope()
    const webrtc = scope.run(() => useWebRTC({
      onTranscript: vi.fn(),
      onReply: vi.fn(),
    }))
    expect(webrtc).toBeDefined()
    await webrtc!.connect("019d0000-0000-7000-8000-000000000025")

    const peer = harness.FakePeerConnection.instances[0]!
    const remote = makeRemoteStream()
    peer.ontrack?.({ track: remote.track, streams: [remote.stream] } as unknown as RTCTrackEvent)
    await vi.waitFor(() => expect(requestFrame).toHaveBeenCalledOnce())

    harness.analyser.getByteFrequencyData.mockImplementation(() => {
      throw analyserError
    })
    const frameCallback = frames.get(1)
    expect(frameCallback).toBeDefined()
    frames.delete(1)
    frameCallback?.(performance.now())

    await vi.waitFor(() => {
      expect(reportClientEvent).toHaveBeenCalledWith(
        "webrtc_remote_audio_error",
        analyserError.message,
      )
      expect(peer.close).toHaveBeenCalledOnce()
    })
    expect(requestFrame).toHaveBeenCalledOnce()
    expect(cancelFrame).not.toHaveBeenCalled()
    expect(harness.disconnectSource).toHaveBeenCalledOnce()
    expect(harness.disconnectAnalyser).toHaveBeenCalledOnce()
    expect(remote.stopTrack).toHaveBeenCalledOnce()
    expect(harness.closeContext).toHaveBeenCalledOnce()

    const reportCount = vi.mocked(reportClientEvent).mock.calls.length
    frameCallback?.(performance.now())
    await flushOwnedOperations()
    expect(vi.mocked(reportClientEvent).mock.calls).toHaveLength(reportCount)
    expect(requestFrame).toHaveBeenCalledOnce()

    scope.stop()
    await flushOwnedOperations()
  })

  it("cancels the exact animation frame when teardown re-enters frame acquisition", async () => {
    const harness = installConnectedHarness()
    vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue()
    vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => {})
    vi.spyOn(HTMLMediaElement.prototype, "load").mockImplementation(() => {})
    const exactFrameId = 947
    const cancelFrame = vi.fn()
    let disconnecting: Promise<void> | null = null
    let webrtc: ReturnType<typeof useWebRTC> | undefined
    vi.stubGlobal("requestAnimationFrame", vi.fn(() => {
      disconnecting = webrtc!.disconnect()
      return exactFrameId
    }))
    vi.stubGlobal("cancelAnimationFrame", cancelFrame)

    const scope = effectScope()
    webrtc = scope.run(() => useWebRTC({
      onTranscript: vi.fn(),
      onReply: vi.fn(),
    }))
    expect(webrtc).toBeDefined()
    await webrtc!.connect("019d0000-0000-7000-8000-000000000027")

    const peer = harness.FakePeerConnection.instances[0]!
    const remote = makeRemoteStream()
    peer.ontrack?.({ track: remote.track, streams: [remote.stream] } as unknown as RTCTrackEvent)

    await vi.waitFor(() => expect(disconnecting).not.toBeNull())
    await disconnecting!
    expect(cancelFrame).toHaveBeenCalledWith(exactFrameId)
    expect(harness.disconnectSource).toHaveBeenCalledOnce()
    expect(harness.disconnectAnalyser).toHaveBeenCalledOnce()
    expect(remote.stopTrack).toHaveBeenCalledOnce()
    expect(reportClientEvent).not.toHaveBeenCalledWith(
      "webrtc_cleanup_error",
      expect.stringContaining("remote amplitude frame acquisition"),
    )

    scope.stop()
    await flushOwnedOperations()
  })

  it("transactionally rolls back amplitude nodes when graph setup throws", async () => {
    const harness = installConnectedHarness({
      sourceConnectError: new Error("source connect failed"),
    })
    vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue()
    vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => {})
    vi.spyOn(HTMLMediaElement.prototype, "load").mockImplementation(() => {})

    const scope = effectScope()
    const webrtc = scope.run(() => useWebRTC({
      onTranscript: vi.fn(),
      onReply: vi.fn(),
    }))
    expect(webrtc).toBeDefined()
    await webrtc!.connect("019d0000-0000-7000-8000-000000000008")

    const peer = harness.FakePeerConnection.instances[0]!
    const remote = makeRemoteStream()
    peer.ontrack?.({ track: remote.track, streams: [remote.stream] } as unknown as RTCTrackEvent)

    await vi.waitFor(() => {
      expect(reportClientEvent).toHaveBeenCalledWith(
        "webrtc_remote_audio_error",
        "source connect failed",
      )
    })
    expect(harness.disconnectSource).toHaveBeenCalledOnce()
    expect(harness.disconnectAnalyser).toHaveBeenCalledOnce()
    expect(remote.stopTrack).toHaveBeenCalledOnce()
    expect(peer.close).toHaveBeenCalledOnce()
    expect(harness.closeContext).toHaveBeenCalledOnce()

    scope.stop()
    await flushOwnedOperations()
    expect(harness.disconnectSource).toHaveBeenCalledOnce()
    expect(harness.disconnectAnalyser).toHaveBeenCalledOnce()
  })

  it("publishes the whole amplitude graph before source connection can re-enter teardown", async () => {
    const harness = installConnectedHarness()
    vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue()
    vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => {})
    vi.spyOn(HTMLMediaElement.prototype, "load").mockImplementation(() => {})

    let graphConnected = false
    let disconnecting: Promise<void> | null = null
    let webrtc: ReturnType<typeof useWebRTC> | undefined
    harness.disconnectSource.mockImplementation(() => {
      graphConnected = false
    })
    harness.source.connect.mockImplementation(() => {
      disconnecting = webrtc!.disconnect()
      graphConnected = true
    })

    const scope = effectScope()
    webrtc = scope.run(() => useWebRTC({
      onTranscript: vi.fn(),
      onReply: vi.fn(),
    }))
    expect(webrtc).toBeDefined()
    await webrtc!.connect("019d0000-0000-7000-8000-000000000040")

    const peer = harness.FakePeerConnection.instances[0]!
    const remote = makeRemoteStream()
    peer.ontrack?.({ track: remote.track, streams: [remote.stream] } as unknown as RTCTrackEvent)

    await vi.waitFor(() => expect(disconnecting).not.toBeNull())
    await disconnecting!
    expect(graphConnected).toBe(false)
    expect(harness.disconnectSource).toHaveBeenCalledOnce()
    expect(harness.disconnectAnalyser).toHaveBeenCalledOnce()
    expect(remote.stopTrack).toHaveBeenCalledOnce()

    scope.stop()
    await flushOwnedOperations()
  })

  it("waits for the owned AudioContext close before constructing a replacement", async () => {
    const pendingClose = deferred<void>()
    const harness = installConnectedHarness({ closePromise: pendingClose.promise })
    const scope = effectScope()
    const webrtc = scope.run(() => useWebRTC({
      onTranscript: vi.fn(),
      onReply: vi.fn(),
    }))
    expect(webrtc).toBeDefined()

    await webrtc!.connect("019d0000-0000-7000-8000-000000000009")
    expect(harness.FakeAudioContext.instances).toHaveLength(1)
    const disconnecting = webrtc!.disconnect()

    const reconnecting = webrtc!.connect("019d0000-0000-7000-8000-000000000010")
    await flushMicrotasks()
    expect(harness.FakeAudioContext.instances).toHaveLength(1)
    expect(harness.getUserMedia).toHaveBeenCalledOnce()

    pendingClose.resolve()
    await Promise.all([disconnecting, reconnecting])
    expect(harness.FakeAudioContext.instances).toHaveLength(2)
    expect(harness.getUserMedia).toHaveBeenCalledTimes(2)

    scope.stop()
    await flushOwnedOperations()
    expect(harness.closeContext).toHaveBeenCalledTimes(2)
  })

  it("retains a failed AudioContext close and retries that exact context before reconnecting", async () => {
    const retryClose = deferred<void>()
    const harness = installConnectedHarness()
    harness.closeContext
      .mockRejectedValueOnce(new Error("context close failed"))
      .mockImplementationOnce(() => retryClose.promise)

    const scope = effectScope()
    const webrtc = scope.run(() => useWebRTC({
      onTranscript: vi.fn(),
      onReply: vi.fn(),
    }))
    expect(webrtc).toBeDefined()

    await webrtc!.connect("019d0000-0000-7000-8000-000000000016")
    const originalContext = harness.FakeAudioContext.instances[0]!
    await expect(webrtc!.disconnect()).rejects.toThrow("WebRTC teardown failed")
    expect(originalContext.close).toHaveBeenCalledOnce()
    expect(harness.FakeAudioContext.instances).toHaveLength(1)

    const reconnecting = webrtc!.connect("019d0000-0000-7000-8000-000000000017")
    await vi.waitFor(() => expect(originalContext.close).toHaveBeenCalledTimes(2))
    expect(harness.FakeAudioContext.instances).toHaveLength(1)
    expect(harness.getUserMedia).toHaveBeenCalledOnce()

    retryClose.resolve()
    await expect(reconnecting).resolves.toBeUndefined()
    expect(harness.FakeAudioContext.instances).toHaveLength(2)
    expect(harness.getUserMedia).toHaveBeenCalledTimes(2)

    await expect(webrtc!.disconnect()).resolves.toBeUndefined()
    scope.stop()
    await flushOwnedOperations()
  })

  it("retries only the failed cleanup action before constructing a replacement", async () => {
    const harness = installConnectedHarness()
    harness.stopLocalTrack.mockImplementationOnce(() => {
      throw new Error("track stop failed")
    })

    const scope = effectScope()
    const webrtc = scope.run(() => useWebRTC({
      onTranscript: vi.fn(),
      onReply: vi.fn(),
    }))
    expect(webrtc).toBeDefined()

    await webrtc!.connect("019d0000-0000-7000-8000-000000000018")
    const originalPeer = harness.FakePeerConnection.instances[0]!
    await expect(webrtc!.disconnect()).rejects.toThrow("WebRTC teardown failed")

    expect(harness.stopLocalTrack).toHaveBeenCalledOnce()
    expect(originalPeer.close).toHaveBeenCalledOnce()
    expect(originalPeer.channel.close).toHaveBeenCalledOnce()
    expect(harness.closeContext).toHaveBeenCalledOnce()

    await expect(
      webrtc!.connect("019d0000-0000-7000-8000-000000000019"),
    ).resolves.toBeUndefined()
    expect(harness.stopLocalTrack).toHaveBeenCalledTimes(2)
    expect(originalPeer.close).toHaveBeenCalledOnce()
    expect(originalPeer.channel.close).toHaveBeenCalledOnce()
    expect(harness.closeContext).toHaveBeenCalledOnce()
    expect(harness.FakePeerConnection.instances).toHaveLength(2)

    await expect(webrtc!.disconnect()).resolves.toBeUndefined()
    scope.stop()
    await flushOwnedOperations()
  })

  it("makes a client-ready send failure terminal instead of leaving an orphan connection", async () => {
    const harness = installConnectedHarness({
      dataChannelSendError: new Error("channel send failed"),
    })
    vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue()
    vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => {})
    vi.spyOn(HTMLMediaElement.prototype, "load").mockImplementation(() => {})

    const scope = effectScope()
    const webrtc = scope.run(() => useWebRTC({
      onTranscript: vi.fn(),
      onReply: vi.fn(),
    }))
    expect(webrtc).toBeDefined()
    await webrtc!.connect("019d0000-0000-7000-8000-000000000011")

    const peer = harness.FakePeerConnection.instances[0]!
    const remote = makeRemoteStream()
    peer.ontrack?.({ track: remote.track, streams: [remote.stream] } as unknown as RTCTrackEvent)

    await vi.waitFor(() => {
      expect(reportClientEvent).toHaveBeenCalledWith(
        "webrtc_data_channel_send_error",
        "channel send failed",
      )
    })
    expect(webrtc!.state.value).toBe("error")
    expect(harness.createMediaStreamSource).not.toHaveBeenCalled()
    expect(peer.close).toHaveBeenCalledOnce()
    expect(peer.channel.close).toHaveBeenCalledOnce()
    expect(remote.stopTrack).toHaveBeenCalledOnce()
    expect(harness.closeContext).toHaveBeenCalledOnce()

    scope.stop()
    await flushOwnedOperations()
  })

  it("makes a closed data channel terminal before creating amplitude resources", async () => {
    const harness = installConnectedHarness({ dataChannelState: "closed" })
    vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue()
    vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => {})
    vi.spyOn(HTMLMediaElement.prototype, "load").mockImplementation(() => {})

    const scope = effectScope()
    const webrtc = scope.run(() => useWebRTC({
      onTranscript: vi.fn(),
      onReply: vi.fn(),
    }))
    expect(webrtc).toBeDefined()
    await webrtc!.connect("019d0000-0000-7000-8000-000000000015")

    const peer = harness.FakePeerConnection.instances[0]!
    const remote = makeRemoteStream()
    peer.ontrack?.({ track: remote.track, streams: [remote.stream] } as unknown as RTCTrackEvent)

    await vi.waitFor(() => {
      expect(reportClientEvent).toHaveBeenCalledWith(
        "webrtc_data_channel_unavailable",
        "Data channel state: closed",
      )
    })
    expect(webrtc!.state.value).toBe("error")
    expect(harness.createMediaStreamSource).not.toHaveBeenCalled()
    expect(remote.stopTrack).toHaveBeenCalledOnce()
    expect(peer.close).toHaveBeenCalledOnce()

    scope.stop()
    await flushOwnedOperations()
  })

  it("separates malformed messages from callback failures and keeps both observable", async () => {
    const transcriptError = new Error("transcript callback failed")
    const harness = installConnectedHarness()
    const scope = effectScope()
    const webrtc = scope.run(() => useWebRTC({
      onTranscript: () => { throw transcriptError },
      onReply: vi.fn(),
    }))
    expect(webrtc).toBeDefined()
    await webrtc!.connect("019d0000-0000-7000-8000-000000000012")

    const channel = harness.FakePeerConnection.instances[0]!.channel
    channel.onmessage?.(new MessageEvent("message", { data: "{" }))
    channel.onmessage?.(new MessageEvent("message", {
      data: JSON.stringify({ type: "transcript", text: "hello" }),
    }))

    expect(reportClientEvent).toHaveBeenCalledWith(
      "webrtc_message_parse_error",
      expect.any(String),
    )
    expect(reportClientEvent).toHaveBeenCalledWith(
      "webrtc_callback_error",
      "transcript: transcript callback failed",
    )
    expect(channel.close).not.toHaveBeenCalled()

    scope.stop()
    await flushOwnedOperations()
    expect(channel.close).toHaveBeenCalledOnce()
  })

  it("rolls back a partially constructed remote audio element", async () => {
    const harness = installConnectedHarness()
    const append = document.body.appendChild.bind(document.body)
    const appendChild = vi.spyOn(document.body, "appendChild")
      .mockImplementation((node) => {
        if (node instanceof HTMLAudioElement) throw new Error("append failed")
        return append(node)
      })
    const remove = vi.spyOn(HTMLAudioElement.prototype, "remove")

    const scope = effectScope()
    const webrtc = scope.run(() => useWebRTC({
      onTranscript: vi.fn(),
      onReply: vi.fn(),
    }))
    expect(webrtc).toBeDefined()
    await webrtc!.connect("019d0000-0000-7000-8000-000000000013")

    const peer = harness.FakePeerConnection.instances[0]!
    const remote = makeRemoteStream()
    peer.ontrack?.({ track: remote.track, streams: [remote.stream] } as unknown as RTCTrackEvent)

    await vi.waitFor(() => {
      expect(reportClientEvent).toHaveBeenCalledWith(
        "webrtc_remote_audio_error",
        "append failed",
      )
    })
    expect(appendChild).toHaveBeenCalledOnce()
    expect(remove).toHaveBeenCalledOnce()
    expect(remote.stopTrack).toHaveBeenCalledOnce()
    expect(peer.close).toHaveBeenCalledOnce()

    scope.stop()
    await flushOwnedOperations()
    expect(remove).toHaveBeenCalledOnce()
  })

  it("removes a remote audio element when teardown re-enters DOM insertion", async () => {
    const harness = installConnectedHarness()
    const append = document.body.appendChild.bind(document.body)
    const remove = vi.spyOn(HTMLAudioElement.prototype, "remove")
    let disconnecting: Promise<void> | null = null
    let webrtc: ReturnType<typeof useWebRTC> | undefined
    vi.spyOn(document.body, "appendChild").mockImplementation((node) => {
      const appended = append(node)
      if (node instanceof HTMLAudioElement) disconnecting = webrtc!.disconnect()
      return appended
    })

    const scope = effectScope()
    webrtc = scope.run(() => useWebRTC({
      onTranscript: vi.fn(),
      onReply: vi.fn(),
    }))
    expect(webrtc).toBeDefined()
    await webrtc!.connect("019d0000-0000-7000-8000-000000000028")

    const peer = harness.FakePeerConnection.instances[0]!
    const remote = makeRemoteStream()
    peer.ontrack?.({ track: remote.track, streams: [remote.stream] } as unknown as RTCTrackEvent)

    await vi.waitFor(() => expect(disconnecting).not.toBeNull())
    await disconnecting!
    expect(remove).toHaveBeenCalledOnce()
    expect(document.querySelector("audio")).toBeNull()
    expect(remote.stopTrack).toHaveBeenCalledOnce()
    expect(peer.close).toHaveBeenCalledOnce()
    expect(reportClientEvent).not.toHaveBeenCalledWith(
      "webrtc_cleanup_error",
      expect.stringContaining("remote audio element construction"),
    )

    scope.stop()
    await flushOwnedOperations()
  })

  it("stops an untransferred remote track when fallback MediaStream construction fails", async () => {
    const harness = installConnectedHarness()
    const stopTrack = vi.fn()
    class BrokenMediaStream {
      constructor() {
        throw new Error("stream construction failed")
      }
    }
    vi.stubGlobal("MediaStream", BrokenMediaStream)

    const scope = effectScope()
    const webrtc = scope.run(() => useWebRTC({
      onTranscript: vi.fn(),
      onReply: vi.fn(),
    }))
    expect(webrtc).toBeDefined()
    await webrtc!.connect("019d0000-0000-7000-8000-000000000014")

    const peer = harness.FakePeerConnection.instances[0]!
    peer.ontrack?.({
      track: { stop: stopTrack },
      streams: [],
    } as unknown as RTCTrackEvent)

    await vi.waitFor(() => {
      expect(reportClientEvent).toHaveBeenCalledWith(
        "webrtc_remote_audio_error",
        "stream construction failed",
      )
    })
    expect(stopTrack).toHaveBeenCalledOnce()
    expect(peer.close).toHaveBeenCalledOnce()
    expect(harness.closeContext).toHaveBeenCalledOnce()

    scope.stop()
    await flushOwnedOperations()
    expect(stopTrack).toHaveBeenCalledOnce()
  })

  it("joins a pending microphone acquisition before disconnect resolves", async () => {
    const microphone = deferred<MediaStream>()
    const stopTrack = vi.fn()
    const stream = {
      getTracks: () => [{ stop: stopTrack }],
    } as unknown as MediaStream
    const getUserMedia = vi.fn(() => microphone.promise)
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: { getUserMedia },
    })
    vi.stubGlobal("fetch", vi.fn())

    const scope = effectScope()
    const webrtc = scope.run(() => useWebRTC({
      onTranscript: vi.fn(),
      onReply: vi.fn(),
    }))
    expect(webrtc).toBeDefined()

    const connecting = webrtc!.connect("019d0000-0000-7000-8000-000000000029")
    await flushMicrotasks()
    expect(getUserMedia).toHaveBeenCalledOnce()

    let disconnected = false
    const disconnecting = webrtc!.disconnect().then(() => {
      disconnected = true
    })
    await flushMicrotasks()
    expect(disconnected).toBe(false)
    expect(stopTrack).not.toHaveBeenCalled()

    microphone.resolve(stream)
    await Promise.all([connecting, disconnecting])
    expect(stopTrack).toHaveBeenCalledOnce()
    expect(webrtc!.state.value).toBe("disconnected")

    scope.stop()
    await flushOwnedOperations()
    expect(stopTrack).toHaveBeenCalledOnce()
  })

  it("lets application teardown join a scope-disposed pending WebRTC acquisition", async () => {
    const microphone = deferred<MediaStream>()
    const stopTrack = vi.fn()
    const stream = {
      getTracks: () => [{ stop: stopTrack }],
    } as unknown as MediaStream
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: { getUserMedia: vi.fn(() => microphone.promise) },
    })
    vi.stubGlobal("fetch", vi.fn())

    const scope = effectScope()
    const webrtc = scope.run(() => useWebRTC({
      onTranscript: vi.fn(),
      onReply: vi.fn(),
    }))
    expect(webrtc).toBeDefined()

    const connecting = webrtc!.connect("019d0000-0000-7000-8000-000000000030")
    await flushMicrotasks()
    scope.stop()

    let applicationSettled = false
    const applicationTeardown = settleRetainedAsynchronousTeardowns().then(() => {
      applicationSettled = true
    })
    await flushMicrotasks()
    expect(applicationSettled).toBe(false)

    microphone.resolve(stream)
    await Promise.all([connecting, applicationTeardown])
    expect(stopTrack).toHaveBeenCalledOnce()
  })

  it("publishes connection cancellation before AbortController construction can re-enter disconnect", async () => {
    const harness = installConnectedHarness()
    const NativeAbortController = AbortController
    const abort = vi.fn()
    let disconnecting: Promise<void> | null = null
    let webrtc: ReturnType<typeof useWebRTC> | undefined

    class ReentrantAbortController extends NativeAbortController {
      static instances: ReentrantAbortController[] = []

      constructor() {
        super()
        ReentrantAbortController.instances.push(this)
        if (ReentrantAbortController.instances.length === 1) {
          disconnecting = webrtc!.disconnect()
        }
      }

      override abort(reason?: unknown): void {
        abort(this, reason)
        super.abort(reason)
      }
    }
    vi.stubGlobal("AbortController", ReentrantAbortController)

    const scope = effectScope()
    webrtc = scope.run(() => useWebRTC({
      onTranscript: vi.fn(),
      onReply: vi.fn(),
    }))
    expect(webrtc).toBeDefined()

    const connecting = webrtc!.connect("019d0000-0000-7000-8000-000000000037")
    await vi.waitFor(() => expect(disconnecting).not.toBeNull())
    await Promise.all([connecting, disconnecting!])

    const controller = ReentrantAbortController.instances[0]!
    expect(abort).toHaveBeenCalledOnce()
    expect(abort.mock.calls[0]![0]).toBe(controller)
    expect(controller.signal.aborted).toBe(true)
    expect(harness.getUserMedia).not.toHaveBeenCalled()
    expect(harness.fetchMock).not.toHaveBeenCalled()
    expect(harness.FakePeerConnection.instances).toHaveLength(0)

    scope.stop()
    await flushOwnedOperations()
  })

  it("fails teardown promptly when connection cancellation leaves pending work live", async () => {
    const harness = installConnectedHarness()
    const NativeAbortController = AbortController
    const abortFailure = new Error("connection abort failed before mutation")

    class RetryableAbortController extends NativeAbortController {
      static instances: RetryableAbortController[] = []
      calls = 0

      constructor() {
        super()
        RetryableAbortController.instances.push(this)
      }

      override abort(reason?: unknown): void {
        this.calls++
        if (this === RetryableAbortController.instances[0] && this.calls === 1) {
          throw abortFailure
        }
        super.abort(reason)
      }
    }
    vi.stubGlobal("AbortController", RetryableAbortController)

    let requestSignal: AbortSignal | null = null
    const pendingFetch = vi.fn((_input: RequestInfo | URL, init?: RequestInit) => {
      requestSignal = init?.signal ?? null
      return new Promise<Response>((_resolve, reject) => {
        requestSignal?.addEventListener("abort", () => reject(requestSignal?.reason), { once: true })
      })
    })
    vi.stubGlobal("fetch", pendingFetch)

    const scope = effectScope()
    const webrtc = scope.run(() => useWebRTC({
      onTranscript: vi.fn(),
      onReply: vi.fn(),
    }))
    expect(webrtc).toBeDefined()

    const connecting = webrtc!.connect("019d0000-0000-7000-8000-000000000038")
    await vi.waitFor(() => expect(pendingFetch).toHaveBeenCalledOnce())

    await expect(webrtc!.disconnect()).rejects.toThrow("WebRTC teardown failed")
    const controller = RetryableAbortController.instances[0]!
    expect(controller.calls).toBe(1)
    expect(controller.signal.aborted).toBe(false)
    expect(requestSignal?.aborted).toBe(false)
    expect(harness.stopLocalTrack).toHaveBeenCalledOnce()
    expect(harness.closeContext).toHaveBeenCalledOnce()

    const disconnecting = webrtc!.disconnect()
    await Promise.all([connecting, disconnecting])
    expect(controller.calls).toBe(2)
    expect(controller.signal.aborted).toBe(true)

    scope.stop()
    await flushOwnedOperations()
  })

  it("owns an AudioContext before its constructor can re-enter disconnect", async () => {
    const harness = installConnectedHarness()
    const closeContext = vi.fn(async () => {})
    let disconnecting: Promise<void> | null = null
    let webrtc: ReturnType<typeof useWebRTC> | undefined

    class ReentrantAudioContext {
      state: AudioContextState = "running"
      close = closeContext

      constructor() {
        disconnecting = webrtc!.disconnect()
      }
    }
    vi.stubGlobal("AudioContext", ReentrantAudioContext)

    const scope = effectScope()
    webrtc = scope.run(() => useWebRTC({
      onTranscript: vi.fn(),
      onReply: vi.fn(),
    }))
    expect(webrtc).toBeDefined()

    const connecting = webrtc!.connect("019d0000-0000-7000-8000-000000000031")
    await vi.waitFor(() => expect(disconnecting).not.toBeNull())
    await Promise.all([connecting, disconnecting!])
    expect(closeContext).toHaveBeenCalledOnce()
    expect(harness.stopLocalTrack).toHaveBeenCalledOnce()
    expect(harness.FakePeerConnection.instances).toHaveLength(0)

    scope.stop()
    await flushOwnedOperations()
  })

  it("owns a peer before its constructor can re-enter disconnect", async () => {
    const harness = installConnectedHarness()
    let disconnecting: Promise<void> | null = null
    let webrtc: ReturnType<typeof useWebRTC> | undefined

    class ReentrantPeerConnection extends harness.FakePeerConnection {
      constructor() {
        super()
        disconnecting = webrtc!.disconnect()
      }
    }
    vi.stubGlobal("RTCPeerConnection", ReentrantPeerConnection)

    const scope = effectScope()
    webrtc = scope.run(() => useWebRTC({
      onTranscript: vi.fn(),
      onReply: vi.fn(),
    }))
    expect(webrtc).toBeDefined()

    const connecting = webrtc!.connect("019d0000-0000-7000-8000-000000000032")
    await vi.waitFor(() => expect(disconnecting).not.toBeNull())
    await Promise.all([connecting, disconnecting!])
    const peer = harness.FakePeerConnection.instances[0]!
    expect(peer.close).toHaveBeenCalledOnce()
    expect(peer.createDataChannel).not.toHaveBeenCalled()
    expect(harness.stopLocalTrack).toHaveBeenCalledOnce()
    expect(harness.closeContext).toHaveBeenCalledOnce()

    scope.stop()
    await flushOwnedOperations()
  })

  it("owns a data channel before creation can re-enter disconnect", async () => {
    const harness = installConnectedHarness()
    let disconnecting: Promise<void> | null = null
    let webrtc: ReturnType<typeof useWebRTC> | undefined

    class ReentrantDataChannelPeer extends harness.FakePeerConnection {
      constructor() {
        super()
        this.createDataChannel = vi.fn(() => {
          disconnecting = webrtc!.disconnect()
          return this.channel
        })
      }
    }
    vi.stubGlobal("RTCPeerConnection", ReentrantDataChannelPeer)

    const scope = effectScope()
    webrtc = scope.run(() => useWebRTC({
      onTranscript: vi.fn(),
      onReply: vi.fn(),
    }))
    expect(webrtc).toBeDefined()

    const connecting = webrtc!.connect("019d0000-0000-7000-8000-000000000033")
    await vi.waitFor(() => expect(disconnecting).not.toBeNull())
    await Promise.all([connecting, disconnecting!])
    const peer = harness.FakePeerConnection.instances[0]!
    expect(peer.channel.close).toHaveBeenCalledOnce()
    expect(peer.close).toHaveBeenCalledOnce()
    expect(harness.stopLocalTrack).toHaveBeenCalledOnce()
    expect(harness.closeContext).toHaveBeenCalledOnce()

    scope.stop()
    await flushOwnedOperations()
  })

  it("publishes callback ownership before a property setter can re-enter teardown", async () => {
    const harness = installConnectedHarness()
    const onTranscript = vi.fn()
    let disconnecting: Promise<void> | null = null
    let webrtc: ReturnType<typeof useWebRTC> | undefined
    let cachedMessageCallback: ((event: MessageEvent) => void) | null = null

    class ReentrantCallbackPeer extends harness.FakePeerConnection {
      constructor() {
        super()
        let openCallback: (() => void) | null = null
        let messageCallback: ((event: MessageEvent) => void) | null = null
        Object.defineProperty(this.channel, "onopen", {
          configurable: true,
          get: () => openCallback,
          set: (callback: (() => void) | null) => { openCallback = callback },
        })
        Object.defineProperty(this.channel, "onmessage", {
          configurable: true,
          get: () => messageCallback,
          set: (callback: ((event: MessageEvent) => void) | null) => {
            if (callback && disconnecting === null) {
              cachedMessageCallback = callback
              disconnecting = webrtc!.disconnect()
            }
            messageCallback = callback
          },
        })
      }
    }
    vi.stubGlobal("RTCPeerConnection", ReentrantCallbackPeer)

    const scope = effectScope()
    webrtc = scope.run(() => useWebRTC({
      onTranscript,
      onReply: vi.fn(),
    }))
    expect(webrtc).toBeDefined()

    const connecting = webrtc!.connect("019d0000-0000-7000-8000-000000000039")
    await vi.waitFor(() => expect(disconnecting).not.toBeNull())
    await Promise.all([connecting, disconnecting!])

    const peer = harness.FakePeerConnection.instances[0]!
    expect(peer.channel.onopen).toBeNull()
    expect(peer.channel.onmessage).toBeNull()
    expect(peer.ontrack).toBeNull()
    expect(peer.oniceconnectionstatechange).toBeNull()
    expect(peer.channel.close).toHaveBeenCalledOnce()
    expect(peer.close).toHaveBeenCalledOnce()

    cachedMessageCallback?.({
      data: JSON.stringify({ type: "transcript", text: "stale" }),
    } as MessageEvent)
    expect(onTranscript).not.toHaveBeenCalled()

    scope.stop()
    await flushOwnedOperations()
  })

  it("keeps peer close behind an addTrack re-entry critical section", async () => {
    const harness = installConnectedHarness()
    let disconnecting: Promise<void> | null = null
    let webrtc: ReturnType<typeof useWebRTC> | undefined

    class ReentrantAddTrackPeer extends harness.FakePeerConnection {
      activeTracks = 0

      constructor() {
        super()
        this.close = vi.fn(() => { this.activeTracks = 0 })
        this.addTrack = vi.fn(() => {
          disconnecting = webrtc!.disconnect()
          this.activeTracks++
          return this.sender
        })
      }
    }
    vi.stubGlobal("RTCPeerConnection", ReentrantAddTrackPeer)

    const scope = effectScope()
    webrtc = scope.run(() => useWebRTC({
      onTranscript: vi.fn(),
      onReply: vi.fn(),
    }))
    expect(webrtc).toBeDefined()

    const connecting = webrtc!.connect("019d0000-0000-7000-8000-000000000040")
    await vi.waitFor(() => expect(disconnecting).not.toBeNull())
    await Promise.all([connecting, disconnecting!])

    const peer = harness.FakePeerConnection.instances[0] as ReentrantAddTrackPeer
    expect(peer.addTrack).toHaveBeenCalledOnce()
    expect(peer.activeTracks).toBe(0)
    expect(peer.createDataChannel).not.toHaveBeenCalled()
    expect(peer.close).toHaveBeenCalledOnce()

    scope.stop()
    await flushOwnedOperations()
  })

  it("keeps peer close behind a transceiver mutation re-entry critical section", async () => {
    const harness = installConnectedHarness()
    let disconnecting: Promise<void> | null = null
    let webrtc: ReturnType<typeof useWebRTC> | undefined

    class ReentrantTransceiverPeer extends harness.FakePeerConnection {
      transceiverActive = false

      constructor() {
        super()
        const peer = this
        const transceiver = {
          sender: this.sender,
          get direction() { return "sendonly" as RTCRtpTransceiverDirection },
          set direction(_direction: RTCRtpTransceiverDirection) {
            disconnecting = webrtc!.disconnect()
            peer.transceiverActive = true
          },
        } as RTCTransceiver
        this.getTransceivers = vi.fn(() => [transceiver])
        this.close = vi.fn(() => { this.transceiverActive = false })
      }
    }
    vi.stubGlobal("RTCPeerConnection", ReentrantTransceiverPeer)

    const scope = effectScope()
    webrtc = scope.run(() => useWebRTC({
      onTranscript: vi.fn(),
      onReply: vi.fn(),
    }))
    expect(webrtc).toBeDefined()

    const connecting = webrtc!.connect("019d0000-0000-7000-8000-000000000041")
    await vi.waitFor(() => expect(disconnecting).not.toBeNull())
    await Promise.all([connecting, disconnecting!])

    const peer = harness.FakePeerConnection.instances[0] as ReentrantTransceiverPeer
    expect(peer.transceiverActive).toBe(false)
    expect(peer.createDataChannel).not.toHaveBeenCalled()
    expect(peer.close).toHaveBeenCalledOnce()

    scope.stop()
    await flushOwnedOperations()
  })

  it("publishes fallback stream ownership before construction can re-enter teardown", async () => {
    const harness = installConnectedHarness()
    const trackStopError = new Error("fallback track stop failed")
    const stopTrack = vi.fn()
      .mockImplementationOnce(() => { throw trackStopError })
    const track = { stop: stopTrack } as unknown as MediaStreamTrack
    let teardownResult: Promise<{ error: unknown | null }> | null = null
    let webrtc: ReturnType<typeof useWebRTC> | undefined

    const scope = effectScope()
    webrtc = scope.run(() => useWebRTC({
      onTranscript: vi.fn(),
      onReply: vi.fn(),
    }))
    expect(webrtc).toBeDefined()
    await webrtc!.connect("019d0000-0000-7000-8000-000000000042")

    class ReentrantMediaStream {
      readonly tracks: MediaStreamTrack[]

      constructor(tracks: MediaStreamTrack[]) {
        teardownResult = webrtc!.disconnect().then(
          () => ({ error: null }),
          error => ({ error }),
        )
        this.tracks = tracks
      }

      getTracks() {
        return this.tracks
      }
    }
    vi.stubGlobal("MediaStream", ReentrantMediaStream)

    const peer = harness.FakePeerConnection.instances[0]!
    peer.ontrack?.({ track, streams: [] } as unknown as RTCTrackEvent)
    await vi.waitFor(() => expect(teardownResult).not.toBeNull())

    const firstTeardown = await teardownResult!
    expect(firstTeardown.error).toBeInstanceOf(AggregateError)
    expect((firstTeardown.error as Error).message).toBe("WebRTC teardown failed")
    expect(stopTrack).toHaveBeenCalledOnce()
    await expect(webrtc!.disconnect()).resolves.toBeUndefined()
    expect(stopTrack).toHaveBeenCalledTimes(2)

    scope.stop()
    await flushOwnedOperations()
  })

  it("retains AbortController mutate-then-throw cleanup for the next public attempt", async () => {
    const NativeAbortController = AbortController
    const connectionAbortError = new Error("connection abort failed")

    class RetryingAbortController extends NativeAbortController {
      static instances: RetryingAbortController[] = []
      calls = 0

      constructor() {
        super()
        RetryingAbortController.instances.push(this)
      }

      override abort(reason?: unknown): void {
        this.calls++
        super.abort(reason)
        if (this === RetryingAbortController.instances[0] && this.calls === 1) {
          throw connectionAbortError
        }
      }
    }
    vi.stubGlobal("AbortController", RetryingAbortController)
    installConnectedHarness()

    const scope = effectScope()
    const webrtc = scope.run(() => useWebRTC({
      onTranscript: vi.fn(),
      onReply: vi.fn(),
    }))
    expect(webrtc).toBeDefined()
    await webrtc!.connect("019d0000-0000-7000-8000-000000000034")

    await expect(webrtc!.disconnect()).rejects.toThrow("WebRTC teardown failed")
    const connectionController = RetryingAbortController.instances[0]!
    expect(connectionController.calls).toBe(1)

    await expect(webrtc!.disconnect()).resolves.toBeUndefined()
    expect(connectionController.calls).toBe(2)

    scope.stop()
    await flushOwnedOperations()
  })

  it("retries a failed exact animation-frame cancellation only on a new attempt", async () => {
    const harness = installConnectedHarness()
    vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue()
    vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => {})
    vi.spyOn(HTMLMediaElement.prototype, "load").mockImplementation(() => {})
    const frameCallback = deferred<FrameRequestCallback>()
    const exactFrameId = 953
    vi.stubGlobal("requestAnimationFrame", vi.fn((callback: FrameRequestCallback) => {
      frameCallback.resolve(callback)
      return exactFrameId
    }))
    const cancellationError = new Error("frame cancellation failed")
    const cancelFrame = vi.fn()
      .mockImplementationOnce(() => { throw cancellationError })
    vi.stubGlobal("cancelAnimationFrame", cancelFrame)

    const scope = effectScope()
    const webrtc = scope.run(() => useWebRTC({
      onTranscript: vi.fn(),
      onReply: vi.fn(),
    }))
    expect(webrtc).toBeDefined()
    await webrtc!.connect("019d0000-0000-7000-8000-000000000035")

    const peer = harness.FakePeerConnection.instances[0]!
    const remote = makeRemoteStream()
    peer.ontrack?.({ track: remote.track, streams: [remote.stream] } as unknown as RTCTrackEvent)
    const cachedCallback = await frameCallback.promise

    await expect(webrtc!.disconnect()).rejects.toThrow("WebRTC teardown failed")
    expect(cancelFrame).toHaveBeenCalledTimes(1)
    cachedCallback(performance.now())
    await flushOwnedOperations()
    expect(vi.mocked(requestAnimationFrame)).toHaveBeenCalledOnce()
    expect(cancelFrame).toHaveBeenCalledTimes(1)

    await expect(webrtc!.disconnect()).resolves.toBeUndefined()
    expect(cancelFrame).toHaveBeenCalledTimes(2)
    expect(cancelFrame.mock.calls[1]![0]).toBe(exactFrameId)

    scope.stop()
    await flushOwnedOperations()
  })

  it("rejects with both cleanup and cleanup-reporter failures without an unhandled rejection", async () => {
    const cleanupError = new Error("context close failed")
    const reportingError = new Error("cleanup reporter failed")
    const harness = installConnectedHarness()
    harness.closeContext.mockRejectedValueOnce(cleanupError)
    vi.mocked(reportClientEvent).mockImplementation((type) => {
      if (type === "webrtc_cleanup_error") throw reportingError
    })
    const unhandled = vi.fn()
    window.addEventListener("unhandledrejection", unhandled)

    const scope = effectScope()
    const webrtc = scope.run(() => useWebRTC({
      onTranscript: vi.fn(),
      onReply: vi.fn(),
    }))
    expect(webrtc).toBeDefined()
    await webrtc!.connect("019d0000-0000-7000-8000-000000000036")

    let failure: unknown
    try {
      await webrtc!.disconnect()
    } catch (error) {
      failure = error
    }
    expect(failure).toBeInstanceOf(AggregateError)
    expect((failure as Error).message).toBe("WebRTC cleanup and cleanup reporting failed")
    expect(nestedErrorMessages(failure)).toEqual(expect.arrayContaining([
      expect.stringContaining(cleanupError.message),
      reportingError.message,
    ]))
    await flushOwnedOperations()
    expect(unhandled).not.toHaveBeenCalled()

    await expect(webrtc!.disconnect()).resolves.toBeUndefined()
    window.removeEventListener("unhandledrejection", unhandled)
    scope.stop()
    await flushOwnedOperations()
  })

})
