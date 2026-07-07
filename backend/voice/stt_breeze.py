"""Breeze ASR / Qwen3 ASR Pipecat Service.

Wraps the existing HTTP proxy logic from `api/asr.py` into a Pipecat STTService.
It accumulates PCM audio from the pipeline, converts it to WAV when the user
stops speaking (VAD endpointing), and sends it to the configured ASR endpoint.
"""

import asyncio
import io
import logging
import wave
from collections.abc import AsyncGenerator

from pipecat.frames.frames import AudioRawFrame, Frame, InterruptionFrame, TranscriptionFrame, VADUserStartedSpeakingFrame, VADUserStoppedSpeakingFrame
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.settings import STTSettings
from pipecat.services.stt_service import STTService

from api.asr import _asr_config, _asr_post_audio
from telemetry import get_telemetry

_log = logging.getLogger(__name__)


def pcm_to_wav(pcm_bytes: bytes, sample_rate: int = 16000, num_channels: int = 1, sample_width: int = 2) -> bytes:
    """Convert raw PCM bytes to WAV format in memory."""
    with io.BytesIO() as buf:
        with wave.open(buf, "wb") as wav_file:
            wav_file.setnchannels(num_channels)
            wav_file.setsampwidth(sample_width)
            wav_file.setframerate(sample_rate)
            wav_file.writeframes(pcm_bytes)
        return buf.getvalue()


class BreezeSTTService(STTService):
    """Custom STT Service for Breeze-ASR-26 or Qwen3-ASR."""

    def __init__(self, sample_rate: int = 16000, num_channels: int = 1, **kwargs):
        super().__init__(sample_rate=sample_rate, settings=STTSettings(model=None, language=None), **kwargs)
        self.num_channels = num_channels
        self._audio_buffer = bytearray()
        self._is_speaking = False
        self._stt_task: asyncio.Task | None = None

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, InterruptionFrame):
            if self._stt_task and not self._stt_task.done():
                self._stt_task.cancel()
                self._stt_task = None
            self._audio_buffer.clear()
            self._is_speaking = False

        elif isinstance(frame, VADUserStartedSpeakingFrame):
            self._is_speaking = True
            self._audio_buffer.clear()
            if self._stt_task and not self._stt_task.done():
                self._stt_task.cancel()
                self._stt_task = None

        elif isinstance(frame, AudioRawFrame) and self._is_speaking:
            self._audio_buffer.extend(frame.audio)

        elif isinstance(frame, VADUserStoppedSpeakingFrame):
            self._is_speaking = False
            if self._audio_buffer:
                audio_data = bytes(self._audio_buffer)
                self._audio_buffer.clear()

                # Cancel any dangling task just in case
                if self._stt_task and not self._stt_task.done():
                    self._stt_task.cancel()

                # Run batch STT in a background task to avoid blocking process_frame
                self._stt_task = self.create_task(self._process_and_push_stt(audio_data, direction))

    async def _process_and_push_stt(self, audio_data: bytes, direction: FrameDirection):
        """Helper to run STT and push frames without blocking."""
        async for output_frame in self._run_batch_stt(audio_data):
            if output_frame:
                await self.push_frame(output_frame, direction)

    async def _run_batch_stt(self, audio: bytes) -> AsyncGenerator[Frame | None, None]:
        """Convert accumulated PCM audio to WAV and transcribe via HTTP proxy."""
        if not audio:
            return

        try:
            base_url, model, api_key = _asr_config()
        except Exception as exc:
            # _asr_config() raises HTTPException when env vars are missing.
            # That exception type is meaningless outside an HTTP request context;
            # log and bail so the pipeline degrades gracefully.
            _log.error("ASR config error (check ASR_BASE_URL / ASR_MODEL): %s", exc)
            return

        wav_bytes = pcm_to_wav(
            audio,
            sample_rate=self.sample_rate,
            num_channels=self.num_channels,
            sample_width=2,  # Pipecat uses 16-bit PCM by default
        )

        get_telemetry().record_asr_audio_bytes(len(wav_bytes))

        headers: dict[str, str] = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        try:
            response = await _asr_post_audio(
                url=f"{base_url}/v1/audio/transcriptions",
                headers=headers,
                filename="audio.wav",
                audio_bytes=wav_bytes,
                content_type="audio/wav",
                model=model,
            )
        except Exception:
            _log.exception("ASR request failed")
            return

        if response.status_code != 200:
            _log.error("ASR error %d: %s", response.status_code, response.text)
            return

        try:
            text = response.json().get("text", "").strip()
        except Exception as exc:
            _log.error("ASR response format error: %s", exc)
            return

        if text:
            _log.debug("STT Transcription: %s", text)
            get_telemetry().set_current_content("asr.transcript", text)
            # Pipecat requires user_id and timestamp, we can leave them empty
            yield TranscriptionFrame(text=text, user_id="", timestamp="")

    async def process_audio_frame(self, frame: AudioRawFrame, direction: FrameDirection):
        """Override to prevent STTService from creating useless streaming generators."""
        pass

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame | None, None]:
        """Abstract method required by STTService. Never called since we override process_audio_frame."""
        yield None
