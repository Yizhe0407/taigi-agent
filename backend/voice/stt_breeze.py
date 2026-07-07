"""Breeze ASR / Qwen3 ASR Pipecat Service.

Wraps the existing HTTP proxy logic from `api/asr.py` into a Pipecat
SegmentedSTTService. The base class maintains a 1-second pre-speech rolling
buffer, so the Silero VAD confirmation delay (~200-400ms) no longer silently
drops utterance heads.

Only `run_stt()` is implemented here; VAD event handling, audio buffering, and
WAV packaging are all handled by the parent `SegmentedSTTService`.
"""

import logging
from collections.abc import AsyncGenerator

from pipecat.frames.frames import Frame, TranscriptionFrame
from pipecat.services.settings import STTSettings
from pipecat.services.stt_service import SegmentedSTTService

from api.asr import _asr_config, _asr_post_audio
from telemetry import get_telemetry

_log = logging.getLogger(__name__)


class BreezeSTTService(SegmentedSTTService):
    """Breeze-ASR-26 / Qwen3-ASR STT via HTTP proxy."""

    def __init__(self, sample_rate: int = 16000, **kwargs):
        super().__init__(
            sample_rate=sample_rate,
            settings=STTSettings(model=None, language=None),
            audio_passthrough=False,  # ponytail: downstream needs text only
            **kwargs,
        )

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame | None, None]:
        """Transcribe a complete WAV segment (packaged by SegmentedSTTService)."""
        try:
            base_url, model, api_key = _asr_config()
        except Exception as exc:
            _log.error("ASR config error (check ASR_BASE_URL / ASR_MODEL): %s", exc)
            return

        get_telemetry().record_asr_audio_bytes(len(audio))

        headers: dict[str, str] = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        try:
            response = await _asr_post_audio(
                url=f"{base_url}/v1/audio/transcriptions",
                headers=headers,
                filename="audio.wav",
                audio_bytes=audio,
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
            yield TranscriptionFrame(text=text, user_id="", timestamp="")
