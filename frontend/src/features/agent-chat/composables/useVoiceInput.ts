import { onBeforeUnmount, ref } from "vue"

import { blobToWav } from "@/lib/audio"
import { transcribeAudio } from "../api/chat"

export type VoiceState = "idle" | "recording" | "transcribing"

export function useVoiceInput(onTranscript: (text: string) => void, onError: (msg: string) => void) {
  const voiceState = ref<VoiceState>("idle")
  const micDenied = ref(false)

  let mediaRecorder: MediaRecorder | null = null
  let activeStream: MediaStream | null = null
  let chunks: Blob[] = []
  let autoStopTimer: ReturnType<typeof setTimeout> | null = null
  let destroyed = false

  const MAX_RECORDING_MS = 12_000

  function releaseStream() {
    activeStream?.getTracks().forEach(t => t.stop())
    activeStream = null
  }

  async function start() {
    voiceState.value = "recording" // block re-entry before async gap

    let stream: MediaStream
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true })
    }
    catch (err) {
      voiceState.value = "idle"
      const name = err instanceof DOMException ? err.name : ""
      if (name === "NotAllowedError" || name === "PermissionDeniedError") {
        micDenied.value = true
        onError("麥克風權限被封鎖，請至瀏覽器設定開啟麥克風後重新整理頁面")
      }
      else if (name === "NotFoundError" || name === "DevicesNotFoundError") {
        onError("找不到麥克風裝置")
      }
      else {
        onError("無法啟動麥克風，請再試一次")
      }
      return
    }

    activeStream = stream
    chunks = []

    const recorder = new MediaRecorder(stream)
    const mimeType = recorder.mimeType
    mediaRecorder = recorder

    recorder.ondataavailable = (e) => {
      if (e.data.size > 0) chunks.push(e.data)
    }

    recorder.onstop = async () => {
      releaseStream()

      const raw = new Blob(chunks, { type: mimeType || "audio/webm" })
      chunks = []

      voiceState.value = "transcribing"
      try {
        const wav = await blobToWav(raw)
        const text = await transcribeAudio(wav)
        if (!destroyed) onTranscript(text)
      }
      catch (error) {
        if (!destroyed) onError(error instanceof Error ? error.message : "語音辨識失敗，請再試一次")
      }
      finally {
        if (!destroyed) voiceState.value = "idle"
      }
    }

    recorder.start()
    autoStopTimer = setTimeout(() => stop(), MAX_RECORDING_MS)
  }

  function stop() {
    if (autoStopTimer !== null) {
      clearTimeout(autoStopTimer)
      autoStopTimer = null
    }
    if (mediaRecorder && voiceState.value === "recording") {
      mediaRecorder.stop()
      mediaRecorder = null
    }
  }

  function toggle() {
    if (voiceState.value === "recording") stop()
    else if (voiceState.value === "idle") void start()
  }

  onBeforeUnmount(() => {
    destroyed = true
    stop()
    releaseStream()
  })

  return { voiceState, micDenied, toggle }
}
