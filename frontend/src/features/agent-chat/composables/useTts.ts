import { onBeforeUnmount, readonly, ref } from "vue"

import { synthesizeSpeech } from "../api/chat"

export type TtsState = "idle" | "loading" | "playing"

export function useTts() {
  const ttsState = ref<TtsState>("idle")
  const mouthAmplitude = ref(0)

  let currentAudio: HTMLAudioElement | null = null
  let currentSource: MediaElementAudioSourceNode | null = null
  let currentAnalyser: AnalyserNode | null = null
  let currentBlobUrl: string | null = null
  let currentAbort: AbortController | null = null
  let audioCtx: AudioContext | null = null
  let amplitudeRafId: number | null = null
  let destroyed = false

  function stopAmplitudeSampling() {
    if (amplitudeRafId !== null) {
      cancelAnimationFrame(amplitudeRafId)
      amplitudeRafId = null
    }
    if (!destroyed) mouthAmplitude.value = 0
  }

  function cancel() {
    stopAmplitudeSampling()
    currentAbort?.abort()
    currentAbort = null
    currentSource?.disconnect()
    currentSource = null
    currentAnalyser?.disconnect()
    currentAnalyser = null
    if (currentAudio) {
      currentAudio.pause()
      currentAudio = null
    }
    if (currentBlobUrl) {
      URL.revokeObjectURL(currentBlobUrl)
      currentBlobUrl = null
    }
    if (!destroyed) ttsState.value = "idle"
  }

  // Returns audio duration in ms once metadata loads, null if aborted/failed
  async function speak(text: string): Promise<number | null> {
    cancel()

    if (!audioCtx || audioCtx.state === "closed") {
      audioCtx = new AudioContext()
    }

    const abort = new AbortController()
    currentAbort = abort
    ttsState.value = "loading"

    try {
      if (audioCtx.state === "suspended") {
        await audioCtx.resume()
      }
      const signal = AbortSignal.any([abort.signal, AbortSignal.timeout(15_000)])
      const blob = await synthesizeSpeech(text, signal)
      if (abort.signal.aborted || destroyed) return null

      const url = URL.createObjectURL(blob)
      currentBlobUrl = url
      const audio = new Audio(url)
      currentAudio = audio

      // Resolve with duration (ms) as soon as metadata is available
      const durationMs = await new Promise<number>((resolve) => {
        audio.onloadedmetadata = () => resolve(audio.duration * 1000)
        audio.onerror = () => resolve(0)
        setTimeout(() => resolve(0), 3000)
      })

      // Re-check after async wait — cancel() may have been called during metadata load
      if (abort.signal.aborted || destroyed) return null

      const source = audioCtx.createMediaElementSource(audio)
      const analyser = audioCtx.createAnalyser()
      analyser.fftSize = 512
      analyser.smoothingTimeConstant = 0.2
      analyser.minDecibels = -70
      analyser.maxDecibels = -20
      source.connect(analyser)
      analyser.connect(audioCtx.destination)
      currentSource = source
      currentAnalyser = analyser

      const binHz = audioCtx.sampleRate / analyser.fftSize
      const lowBin = Math.max(0, Math.round(300 / binHz))
      const highBin = Math.min(analyser.frequencyBinCount - 1, Math.round(3400 / binHz))
      const binCount = highBin - lowBin + 1
      const dataArray = new Uint8Array(analyser.frequencyBinCount)

      const NOISE_FLOOR = 0.22

      function sampleAmplitude() {
        analyser.getByteFrequencyData(dataArray)
        let sum = 0
        for (let i = lowBin; i <= highBin; i++) {
          sum += dataArray[i]
        }
        const avg = sum / (binCount * 255)
        if (!destroyed) mouthAmplitude.value = avg < NOISE_FLOOR ? 0 : Math.min(1, (avg - NOISE_FLOOR) * 3.5)
        amplitudeRafId = requestAnimationFrame(sampleAmplitude)
      }

      audio.onended = () => {
        if (currentAudio !== audio) return
        stopAmplitudeSampling()
        currentSource?.disconnect()
        currentSource = null
        currentAnalyser?.disconnect()
        currentAnalyser = null
        URL.revokeObjectURL(url)
        currentBlobUrl = null
        currentAudio = null
        if (!destroyed) ttsState.value = "idle"
      }
      audio.onerror = () => { if (currentAudio === audio) cancel() }

      ttsState.value = "playing"
      amplitudeRafId = requestAnimationFrame(sampleAmplitude)
      await audio.play()
      return durationMs || null
    }
    catch (error) {
      cancel()
      if (error instanceof DOMException && (error.name === "AbortError" || error.name === "TimeoutError")) return null
      return null
    }
  }

  onBeforeUnmount(() => {
    destroyed = true
    cancel()
    audioCtx?.close()
    audioCtx = null
  })

  return { ttsState, mouthAmplitude: readonly(mouthAmplitude), speak, cancel }
}
