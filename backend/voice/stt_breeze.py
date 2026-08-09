"""Breeze ASR / Qwen3 ASR Pipecat Service.

Wraps the shared HTTP proxy logic from `providers/asr.py` (also used by the
REST `/api/asr` endpoint) into a Pipecat SegmentedSTTService. The base class
maintains a 1-second pre-speech rolling buffer, so the Silero VAD
confirmation delay (~200-400ms) no longer silently drops utterance heads.

Only `run_stt()` is implemented here; VAD event handling, audio buffering, and
WAV packaging are all handled by the parent `SegmentedSTTService`.
"""

import logging
from collections.abc import AsyncGenerator

from pipecat.frames.frames import Frame, TranscriptionFrame, TTSSpeakFrame
from pipecat.services.settings import STTSettings
from pipecat.services.stt_service import SegmentedSTTService

from providers.asr import build_asr_headers, get_asr_config, post_asr_audio
from telemetry import get_telemetry

_log = logging.getLogger(__name__)

# Yielded (instead of silently returning) whenever a segment fails to
# transcribe — config missing, upstream request/timeout error, non-200, or an
# unparsable response body. TTSSpeakFrame is pipecat's standalone-utterance
# frame (also used for the pipeline's welcome greeting, see voice/pipeline.py)
# and is handled generically by TaigiTTSService regardless of where it
# originates, so this flows straight through TaigiBusAgentProcessor's frame
# pass-through (it only special-cases TranscriptionFrame/InterruptionFrame)
# into TTS. Without this, a failed ASR call silently drops the user's turn —
# they get no transcript and no reply, and are left wondering if the mic is
# still listening.
_ASR_FAILURE_APOLOGY = "歹勢，我無聽清楚，請閣講一擺。"


class BreezeSTTService(SegmentedSTTService):
    """Breeze-ASR-26 / Qwen3-ASR STT via HTTP proxy."""

    def __init__(self, sample_rate: int = 16000, **kwargs):
        super().__init__(
            sample_rate=sample_rate,
            settings=STTSettings(model=None, language=None),
            audio_passthrough=False,  # ponytail: downstream needs text only
            **kwargs,
        )

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame | None, None]:  # pyright: ignore[reportIncompatibleMethodOverride]
        """Transcribe a complete WAV segment (packaged by SegmentedSTTService)."""
        try:
            base_url, model, api_key = get_asr_config()
        except Exception as exc:
            _log.error("ASR config error (check ASR_BASE_URL / ASR_MODEL): %s", exc)
            yield TTSSpeakFrame(text=_ASR_FAILURE_APOLOGY)
            return

        get_telemetry().record_asr_audio_bytes(len(audio))

        headers = build_asr_headers(api_key)

        try:
            response = await post_asr_audio(
                url=f"{base_url}/v1/audio/transcriptions",
                headers=headers,
                filename="audio.wav",
                audio_bytes=audio,
                content_type="audio/wav",
                model=model,
            )
        except Exception:
            _log.exception("ASR request failed")
            yield TTSSpeakFrame(text=_ASR_FAILURE_APOLOGY)
            return

        if response.status_code != 200:
            _log.error("ASR error %d: %s", response.status_code, response.text)
            yield TTSSpeakFrame(text=_ASR_FAILURE_APOLOGY)
            return

        try:
            text = response.json().get("text", "").strip()
        except Exception as exc:
            _log.error("ASR response format error: %s", exc)
            yield TTSSpeakFrame(text=_ASR_FAILURE_APOLOGY)
            return

        if text:
            _log.debug("STT Transcription: %s", text)
            get_telemetry().set_current_content("asr.transcript", text)
            yield TranscriptionFrame(text=text, user_id="", timestamp="")
