/**
 * Decode any browser-recorded audio blob and re-encode as 16kHz mono WAV.
 * ASR models expect PCM WAV; MediaRecorder produces WebM/Opus which soundfile can't read.
 */
export async function blobToWav(blob: Blob, sampleRate = 16000): Promise<Blob> {
  const ctx = new AudioContext({ sampleRate })
  try {
    const arrayBuffer = await blob.arrayBuffer()
    const audioBuffer = await ctx.decodeAudioData(arrayBuffer)
    const pcm = audioBuffer.getChannelData(0)
    const wavBuffer = encodeWav(pcm, sampleRate)
    return new Blob([wavBuffer], { type: "audio/wav" })
  }
  finally {
    await ctx.close()
  }
}

function encodeWav(samples: Float32Array, sampleRate: number): ArrayBuffer {
  const dataBytes = samples.length * 2 // 16-bit PCM
  const buffer = new ArrayBuffer(44 + dataBytes)
  const view = new DataView(buffer)

  writeAscii(view, 0, "RIFF")
  view.setUint32(4, 36 + dataBytes, true)
  writeAscii(view, 8, "WAVE")
  writeAscii(view, 12, "fmt ")
  view.setUint32(16, 16, true)          // PCM chunk size
  view.setUint16(20, 1, true)           // PCM format
  view.setUint16(22, 1, true)           // mono
  view.setUint32(24, sampleRate, true)
  view.setUint32(28, sampleRate * 2, true)
  view.setUint16(32, 2, true)           // block align
  view.setUint16(34, 16, true)          // bits per sample
  writeAscii(view, 36, "data")
  view.setUint32(40, dataBytes, true)

  for (let i = 0; i < samples.length; i++) {
    const s = Math.max(-1, Math.min(1, samples[i]))
    view.setInt16(44 + i * 2, s < 0 ? s * 0x8000 : s * 0x7FFF, true)
  }

  return buffer
}

function writeAscii(view: DataView, offset: number, text: string) {
  for (let i = 0; i < text.length; i++) {
    view.setUint8(offset + i, text.charCodeAt(i))
  }
}
