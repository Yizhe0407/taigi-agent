/**
 * Decode a browser-recorded audio Blob (webm/opus, ogg, etc.) and re-encode
 * as 16-bit PCM WAV at 16 kHz mono — the native format Whisper-based ASR
 * models expect. Downsampling here avoids sending 3× more data than the model
 * will ever use (device mic is typically 44.1 or 48 kHz).
 *
 * Uses Web Audio API — no server-side ffmpeg required.
 */

const ASR_SAMPLE_RATE = 16_000

export async function blobToWav(blob: Blob): Promise<Blob> {
  const arrayBuffer = await blob.arrayBuffer()

  // Step 1: decode compressed audio (webm/opus, ogg…) to PCM at native rate
  const audioCtx = new AudioContext()
  let decoded: AudioBuffer
  try {
    decoded = await audioCtx.decodeAudioData(arrayBuffer)
  } finally {
    await audioCtx.close()
  }

  // Step 2: resample to 16 kHz via OfflineAudioContext
  const resampled = await resampleTo16k(decoded)

  return audioBufferToWavBlob(resampled)
}

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

async function resampleTo16k(source: AudioBuffer): Promise<AudioBuffer> {
  if (source.sampleRate === ASR_SAMPLE_RATE) return source

  const frames = Math.ceil(source.duration * ASR_SAMPLE_RATE)
  // OfflineAudioContext(channels, frames, sampleRate) performs SRC on render
  const offline = new OfflineAudioContext(1, frames, ASR_SAMPLE_RATE)

  const bufferSource = offline.createBufferSource()
  bufferSource.buffer = source
  bufferSource.connect(offline.destination)
  bufferSource.start(0)

  return offline.startRendering()
}

function audioBufferToWavBlob(audioBuffer: AudioBuffer): Blob {
  const sampleRate = audioBuffer.sampleRate
  const numSamples = audioBuffer.length // already mono from OfflineAudioContext(1,…)

  const channelData = audioBuffer.getChannelData(0)

  // Convert float32 [-1, 1] → int16
  const pcm = new Int16Array(numSamples)
  for (let i = 0; i < numSamples; i++) {
    const s = Math.max(-1, Math.min(1, channelData[i]))
    pcm[i] = s < 0 ? s * 0x8000 : s * 0x7fff
  }

  const dataSize = pcm.byteLength
  const buffer = new ArrayBuffer(44 + dataSize)
  const view = new DataView(buffer)

  writeString(view, 0, "RIFF")
  view.setUint32(4, 36 + dataSize, true)
  writeString(view, 8, "WAVE")
  writeString(view, 12, "fmt ")
  view.setUint32(16, 16, true)       // PCM chunk size
  view.setUint16(20, 1, true)        // PCM format
  view.setUint16(22, 1, true)        // mono
  view.setUint32(24, sampleRate, true)
  view.setUint32(28, sampleRate * 2, true) // byte rate = sr * 1ch * 2 bytes
  view.setUint16(32, 2, true)        // block align
  view.setUint16(34, 16, true)       // bits per sample
  writeString(view, 36, "data")
  view.setUint32(40, dataSize, true)

  const pcmBytes = new Uint8Array(buffer, 44)
  pcmBytes.set(new Uint8Array(pcm.buffer))

  return new Blob([buffer], { type: "audio/wav" })
}

function writeString(view: DataView, offset: number, str: string) {
  for (let i = 0; i < str.length; i++) {
    view.setUint8(offset + i, str.charCodeAt(i))
  }
}
