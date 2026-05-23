import { ref } from "vue"

export type RecorderState = "idle" | "recording" | "processing"

// Energy thresholds on normalized RMS (0–1 before scaling)
const SPEECH_ONSET_RMS = 0.02   // above this counts as "speech started"
const SILENCE_RMS = 0.012       // below this counts as silence
const SILENCE_DURATION_MS = 1500 // consecutive silence before auto-stop
const MIN_RECORDING_MS = 800    // ignore silence detection for the first 800 ms

export interface UseAudioRecorderOptions {
  /**
   * Called with the raw audio Blob when recording ends (auto or manual).
   * The composable transitions back to "idle" after this Promise settles.
   */
  onAudioReady: (blob: Blob) => Promise<void>
}

/**
 * Records audio via MediaRecorder and automatically stops on prolonged silence
 * (energy-based VAD). Exposes a `volume` ref (0–1) for waveform visualisation.
 *
 * State machine: idle → recording → processing → idle
 */
export function useAudioRecorder({ onAudioReady }: UseAudioRecorderOptions) {
  const state = ref<RecorderState>("idle")
  const error = ref<string | null>(null)
  /** Smoothed RMS energy 0–1, updated every animation frame while recording. */
  const volume = ref(0)

  let stream: MediaStream | null = null
  let mediaRecorder: MediaRecorder | null = null
  let audioCtx: AudioContext | null = null
  let analyser: AnalyserNode | null = null
  let animFrame: number | null = null
  let chunks: Blob[] = []

  // Silence-detection state
  let speechDetected = false
  let silenceStart: number | null = null
  let recordingStartTime = 0

  const preferredMimeType =
    MediaRecorder.isTypeSupported("audio/webm;codecs=opus") ? "audio/webm;codecs=opus" :
    MediaRecorder.isTypeSupported("audio/webm") ? "audio/webm" : ""

  // ---------------------------------------------------------------------------
  // Public API
  // ---------------------------------------------------------------------------

  async function startRecording(): Promise<void> {
    if (state.value !== "idle") return
    error.value = null

    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      })
    } catch (e) {
      error.value = e instanceof DOMException && e.name === "NotAllowedError"
        ? "麥克風存取遭拒，請允許瀏覽器存取麥克風"
        : "無法開啟麥克風"
      return
    }

    // AnalyserNode for volume + silence detection
    audioCtx = new AudioContext()
    const source = audioCtx.createMediaStreamSource(stream)
    analyser = audioCtx.createAnalyser()
    analyser.fftSize = 256
    analyser.smoothingTimeConstant = 0.6
    source.connect(analyser)

    speechDetected = false
    silenceStart = null
    recordingStartTime = Date.now()
    _startVolumeLoop()

    chunks = []
    const options = preferredMimeType ? { mimeType: preferredMimeType } : {}
    mediaRecorder = new MediaRecorder(stream, options)
    mediaRecorder.ondataavailable = (e) => { if (e.data.size > 0) chunks.push(e.data) }
    mediaRecorder.onstop = () => {
      const mimeType = mediaRecorder?.mimeType || "audio/webm"
      const blob = new Blob(chunks, { type: mimeType })
      _cleanup()
      state.value = "processing"
      onAudioReady(blob).finally(() => { state.value = "idle" })
    }
    mediaRecorder.start()
    state.value = "recording"
  }

  /** Manually stop recording. Auto-stop fires the same path internally. */
  function stopRecording(): void {
    if (state.value !== "recording" || !mediaRecorder) return
    _stopVolumeLoop()
    mediaRecorder.stop() // triggers onstop → onAudioReady
  }

  /** Emergency cleanup — call on component unmount to release mic. */
  function reset(): void {
    _cleanup()
    state.value = "idle"
  }

  // ---------------------------------------------------------------------------
  // Internal
  // ---------------------------------------------------------------------------

  function _startVolumeLoop() {
    const buf = new Uint8Array(analyser!.frequencyBinCount)

    function tick() {
      analyser!.getByteTimeDomainData(buf)

      // RMS over time-domain samples (128 = zero-crossing in Uint8)
      let sum = 0
      for (let i = 0; i < buf.length; i++) {
        const v = (buf[i] - 128) / 128
        sum += v * v
      }
      const rms = Math.sqrt(sum / buf.length)
      volume.value = Math.min(1, rms * 8) // amplify for visual

      // Silence detection — skip the first MIN_RECORDING_MS to avoid
      // triggering on the click sound or OS mic spin-up noise.
      const elapsed = Date.now() - recordingStartTime
      if (elapsed >= MIN_RECORDING_MS) {
        if (rms > SPEECH_ONSET_RMS) {
          speechDetected = true
          silenceStart = null
        } else if (speechDetected && rms < SILENCE_RMS) {
          if (!silenceStart) {
            silenceStart = Date.now()
          } else if (Date.now() - silenceStart > SILENCE_DURATION_MS) {
            stopRecording() // auto-stop on prolonged silence
            return          // don't schedule next frame
          }
        }
      }

      animFrame = requestAnimationFrame(tick)
    }
    animFrame = requestAnimationFrame(tick)
  }

  function _stopVolumeLoop() {
    if (animFrame !== null) { cancelAnimationFrame(animFrame); animFrame = null }
    volume.value = 0
  }

  function _cleanup() {
    _stopVolumeLoop()
    stream?.getTracks().forEach((t) => t.stop())
    audioCtx?.close()
    stream = null
    audioCtx = null
    analyser = null
    mediaRecorder = null
    chunks = []
    speechDetected = false
    silenceStart = null
  }

  return { state, error, volume, startRecording, stopRecording, reset }
}
