import { onBeforeUnmount, readonly, ref, type Ref } from "vue"

export type WebRTCState = "disconnected" | "connecting" | "connected" | "error"

export function useWebRTC(
  onTranscript: (text: string) => void,
  onReply: (text: string) => void,
  sessionId?: Readonly<Ref<string | null>>,
  onCancelled?: () => void,
) {
  const state = ref<WebRTCState>("disconnected")
  const mouthAmplitude = ref(0)
  const micDenied = ref(false)

  let pc: RTCPeerConnection | null = null
  let localStream: MediaStream | null = null
  let audioCtx: AudioContext | null = null
  let remoteAudio: HTMLAudioElement | null = null
  let analyserRafId: number | null = null
  let pcId: string | null = null
  let destroyed = false

  function stopAmplitude() {
    if (analyserRafId !== null) {
      cancelAnimationFrame(analyserRafId)
      analyserRafId = null
    }
    if (!destroyed) mouthAmplitude.value = 0
  }

  function cleanup() {
    stopAmplitude()
    localStream?.getTracks().forEach(t => t.stop())
    localStream = null
    pc?.close()
    pc = null
    audioCtx?.close().catch(() => {})
    audioCtx = null
    if (remoteAudio) {
      remoteAudio.srcObject = null
      remoteAudio.remove()
      remoteAudio = null
    }
    if (!destroyed) state.value = "disconnected"
  }

  function setupRemoteAmplitude(stream: MediaStream) {
    const ctx = audioCtx!
    // Build the graph and start the loop immediately — do NOT gate on resume().
    // connect() runs after async work (session POST, getUserMedia, ICE), so the
    // user-activation window may have expired and resume() can stay pending
    // forever; gating on it left the mouth frozen. A suspended context just
    // reads zeros until resume succeeds.
    const src = ctx.createMediaStreamSource(stream)
    const analyser = ctx.createAnalyser()
    analyser.fftSize = 512
    analyser.smoothingTimeConstant = 0.2
    analyser.minDecibels = -70
    analyser.maxDecibels = -20
    // Only connect to analyser for amplitude measurement.
    // Do NOT connect to ctx.destination — the WebRTC track already handles playback.
    src.connect(analyser)

    void ctx.resume().catch(() => {})
    if (ctx.state !== "running") {
      // Chrome exempts pages with live mic capture from the autoplay policy, so
      // resume() usually succeeds — but if it doesn't, retry on the next touch.
      const retry = () => void ctx.resume().catch(() => {})
      document.addEventListener("pointerdown", retry, { once: true })
    }

    const binHz = ctx.sampleRate / analyser.fftSize
    const lowBin = Math.max(0, Math.round(300 / binHz))
    const highBin = Math.min(analyser.frequencyBinCount - 1, Math.round(3400 / binHz))
    const binCount = highBin - lowBin + 1
    const buf = new Uint8Array(analyser.frequencyBinCount)
    const NOISE_FLOOR = 0.22

    function tick() {
      analyser.getByteFrequencyData(buf)
      let sum = 0
      for (let i = lowBin; i <= highBin; i++) sum += buf[i]
      const avg = sum / (binCount * 255)
      if (!destroyed) mouthAmplitude.value = avg < NOISE_FLOOR ? 0 : Math.min(1, (avg - NOISE_FLOOR) * 3.5)
      analyserRafId = requestAnimationFrame(tick)
    }
    analyserRafId = requestAnimationFrame(tick)
  }

  let connectId = 0

  async function connect() {
    if (state.value === "connecting" || state.value === "connected") return
    state.value = "connecting"
    pcId = null  // always fresh on reconnect
    const currentId = ++connectId

    let stream: MediaStream
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      })
    } catch (err) {
      if (currentId !== connectId || destroyed) return
      const name = err instanceof DOMException ? err.name : ""
      if (name === "NotAllowedError" || name === "PermissionDeniedError") micDenied.value = true
      state.value = "error"
      return
    }

    if (currentId !== connectId || destroyed) {
      stream.getTracks().forEach(t => t.stop())
      return
    }
    localStream = stream

    // Create AudioContext inside user gesture so it starts un-suspended
    if (!audioCtx || audioCtx.state === "closed") audioCtx = new AudioContext()

    // No STUN: the backend runs on the same host/LAN, so host candidates suffice.
    // A STUN server the kiosk can't reach makes ICE gathering stall until the
    // 3 s timeout below — that was the bulk of the audio start-up delay.
    pc = new RTCPeerConnection({ iceServers: [] })

    localStream.getTracks().forEach(t => {
      const sender = pc!.addTrack(t, localStream!)
      const transceiver = pc!.getTransceivers().find(tr => tr.sender === sender)
      if (transceiver) transceiver.direction = "sendrecv"
    })

    // Backend (pipecat SmallWebRTCConnection) expects client to create data channel
    const dc = pc.createDataChannel("app-messages", { ordered: true })
    dc.onmessage = (evt) => {
      try {
        const msg = JSON.parse(evt.data as string) as { type: string; text?: string }
        if (msg.type === "transcript") onTranscript(msg.text ?? "")
        else if (msg.type === "agent_reply") onReply(msg.text ?? "")
        else if (msg.type === "agent_cancelled") onCancelled?.()
        // unknown types: silently ignored (forward-compat)
      } catch {}
    }

    pc.ontrack = async (evt) => {
      const s = evt.streams?.[0] ?? new MediaStream([evt.track])

      // Play remote audio via a hidden <audio> element.
      // We previously connected analyser → ctx.destination but that caused double-playback.
      // The correct pattern is: hidden audio element for playback + Web Audio API only for analysis.
      if (!remoteAudio) {
        remoteAudio = Object.assign(document.createElement("audio"), {
          autoplay: true,
          playsInline: true,
        })
        // Keep it out of the DOM flow but let the browser actually play it
        remoteAudio.style.display = "none"
        document.body.appendChild(remoteAudio)
      }
      remoteAudio.srcObject = s

      try {
        await remoteAudio.play()
      } catch {
        // Autoplay policy may block play(); the audio element will still
        // buffer and play once user interaction unlocks the context.
      }

      // Notify the server that the audio element has started playing (or at
      // least buffering). The server holds the welcome greeting until it
      // receives this signal, eliminating the PCM-drop race condition.
      const sendReady = () => dc.send(JSON.stringify({ type: "client_ready" }))
      if (dc.readyState === "open") {
        sendReady()
      } else {
        // Data channel opens asynchronously; wait for it rather than fire-and-forget.
        dc.addEventListener("open", sendReady, { once: true })
      }

      setupRemoteAmplitude(s)
    }

    pc.oniceconnectionstatechange = () => {
      if (currentId !== connectId || !pc || destroyed) return
      const s = pc.iceConnectionState
      if (s === "connected" || s === "completed") state.value = "connected"
      else if (s === "failed" || s === "closed") state.value = "error"
    }

    const offer = await pc.createOffer()
    if (currentId !== connectId || destroyed) return
    await pc.setLocalDescription(offer)

    // Wait for ICE gathering before sending offer (SmallWebRTCTransport needs full SDP)
    await new Promise<void>(resolve => {
      if (pc!.iceGatheringState === "complete") { resolve(); return }
      const handler = () => {
        if (pc!.iceGatheringState === "complete") {
          pc!.removeEventListener("icegatheringstatechange", handler)
          resolve()
        }
      }
      pc!.addEventListener("icegatheringstatechange", handler)
      setTimeout(() => { pc?.removeEventListener("icegatheringstatechange", handler); resolve() }, 3000)
    })

    if (currentId !== connectId || destroyed || !pc) return

    let resp: Response
    try {
      resp = await fetch("/api/voice/offer", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          sdp: pc.localDescription!.sdp,
          type: pc.localDescription!.type,
          pc_id: pcId,
          // Pass the existing chat session so voice and text share conversation context.
          session_id: sessionId?.value ?? null,
        }),
      })
    } catch {
      if (currentId !== connectId || destroyed) return
      cleanup()
      state.value = "error"
      return
    }

    if (currentId !== connectId || destroyed) return

    if (!resp.ok) {
      cleanup()
      state.value = "error"
      return
    }

    const answer = await resp.json() as { sdp: string; type: RTCSdpType; pc_id: string }
    pcId = answer.pc_id
    try {
      await pc.setRemoteDescription({ sdp: answer.sdp, type: answer.type })
    } catch {
      if (currentId !== connectId || destroyed) return
      cleanup()
      state.value = "error"
    }
  }

  function disconnect() {
    connectId++ // abort any pending connect
    pcId = null
    cleanup()
  }

  onBeforeUnmount(() => {
    destroyed = true
    cleanup()
  })

  return {
    state: readonly(state),
    mouthAmplitude: readonly(mouthAmplitude),
    micDenied: readonly(micDenied),
    connect,
    disconnect,
  }
}
