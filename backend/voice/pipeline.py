"""Pipecat voice pipeline assembly."""

import asyncio
import json
import logging
from collections import deque

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    InterruptionFrame,
    TTSSpeakFrame,
    VADUserStartedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline

# ponytail: PipelineTask (pipeline.task) is deprecated since 1.3.0 → PipelineWorker
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.audio.vad_processor import VADProcessor
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.transports.base_transport import TransportParams
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
from pipecat.transports.smallwebrtc.transport import SmallWebRTCOutputTransport, SmallWebRTCTransport
from pipecat.workers.base_worker import WorkerParams

from voice.agent_processor import TaigiBusAgentProcessor
from voice.stt_breeze import BreezeSTTService
from voice.tts_taigi import TaigiTTSService

_log = logging.getLogger(__name__)

_WELCOME_TEXT = "請問您欲前往哪裡？"


def _drain_chunk_queue(queue: deque) -> None:
    """Drain aiortc RawAudioTrack._chunk_queue, resolving any pending futures.

    ponytail: deque.clear() leaves in-flight futures un-resolved so their awaiters
    hang forever. Popleft loop resolves each future before discarding the chunk.
    Only the last chunk of each add_audio_bytes() call carries a non-None future.
    """
    while queue:
        _, fut = queue.popleft()
        if fut is not None and not fut.done():
            fut.set_result(True)


class BargeInProcessor(FrameProcessor):
    """Convert VADUserStartedSpeakingFrame → broadcast_interruption(), gated on bot speaking.

    PipelineWorker's LLMResponseUniversalAggregator normally does this, but we
    use a custom agent instead of an LLMService, so we need it explicitly.

    Gate: only interrupt when the bot is currently speaking.
    Rationale: if the bot is silent, agent_processor.py cancels the prior inference
    task on the next TranscriptionFrame anyway — no interruption needed, and
    noise/cough would otherwise kill in-flight reasoning with no recovery path.

    BotStartedSpeakingFrame / BotStoppedSpeakingFrame are broadcast upstream by the
    output transport's MediaSender and travel through this processor on their way to
    the input transport.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._bot_speaking = False

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, BotStartedSpeakingFrame):
            self._bot_speaking = True
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._bot_speaking = False
        elif isinstance(frame, VADUserStartedSpeakingFrame) and self._bot_speaking:
            _log.debug("Barge-in detected while bot speaking, broadcasting interruption")
            await self.broadcast_interruption()
        await self.push_frame(frame, direction)


class _TaigiSmallWebRTCOutputTransport(SmallWebRTCOutputTransport):
    """Extends SmallWebRTCOutputTransport to clear aiortc's audio buffer on barge-in.

    ponytail: pipecat's handle_interruptions() resets its own _audio_queue but
    audio already written to aiortc's RawAudioTrack._chunk_queue keeps playing.
    No public API to clear it — override until upstream adds one.
    """

    async def _handle_frame(self, frame: Frame) -> None:
        if isinstance(frame, InterruptionFrame):
            if self._client and getattr(self._client, "_audio_output_track", None):
                track = self._client._audio_output_track
                chunk_queue = getattr(track, "_chunk_queue", None)
                # aiortc RawAudioTrack uses a collections.deque for _chunk_queue.
                # Guard the type explicitly so a future aiortc refactor doesn't silently no-op.
                if isinstance(chunk_queue, deque):
                    try:
                        _drain_chunk_queue(chunk_queue)
                    except Exception:
                        _log.warning("Could not clear aiortc audio buffer on barge-in")
        await super()._handle_frame(frame)


class _TaigiSmallWebRTCTransport(SmallWebRTCTransport):
    """Swaps in _TaigiSmallWebRTCOutputTransport so barge-in clears aiortc buffer."""

    def output(self) -> _TaigiSmallWebRTCOutputTransport:  # type: ignore[override]
        if not self._output:
            self._output = _TaigiSmallWebRTCOutputTransport(self._client, self._params)
        return self._output  # type: ignore[return-value]


async def run_voice_pipeline(webrtc_connection: SmallWebRTCConnection, session_id: str) -> None:
    """Run the Taigi Bus Agent voice pipeline."""

    transport = _TaigiSmallWebRTCTransport(
        webrtc_connection=webrtc_connection,
        params=TransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            video_in_enabled=False,
            video_out_enabled=False,
        ),
    )

    vad = VADProcessor(vad_analyzer=SileroVADAnalyzer(params=VADParams(stop_secs=0.7)))
    barge_in = BargeInProcessor()
    stt = BreezeSTTService()
    agent = TaigiBusAgentProcessor(
        session_id=session_id,
        send_event=webrtc_connection.send_app_message,
    )
    tts = TaigiTTSService()

    pipeline = Pipeline(
        processors=[
            transport.input(),
            vad,
            barge_in,
            stt,
            agent,
            tts,
            transport.output(),
        ]
    )

    # ponytail: idle_timeout_secs=None — disconnect cancels the worker (fix #1),
    # so the 5-min idle kill is unnecessary and harmful (kills a live connection).
    task = PipelineWorker(pipeline, idle_timeout_secs=None, params=PipelineParams())

    # Event set by on_app_message when the client sends {"type": "client_ready"}.
    # The welcome greeting is held until the frontend confirms its audio element is playing,
    # eliminating the race between server-side TTS output and browser-side audio track setup.
    _client_ready = asyncio.Event()

    @transport.event_handler("on_app_message")
    async def on_app_message(_transport, message, _sender) -> None:
        try:
            data = json.loads(message) if isinstance(message, (str, bytes)) else message
            if isinstance(data, dict) and data.get("type") == "client_ready":
                _log.debug("client_ready received for session %s", session_id)
                _client_ready.set()
        except Exception:
            pass

    @transport.event_handler("on_client_connected")
    async def on_connected(_transport, _connection) -> None:
        async def _send_welcome():
            # Wait for the frontend to confirm its <audio> element has started playing
            # before sending the welcome text. Falls back after 3 s so we always greet.
            try:
                await asyncio.wait_for(_client_ready.wait(), timeout=3.0)
            except TimeoutError:
                _log.warning(
                    "client_ready not received within 3 s for session %s — sending welcome anyway",
                    session_id,
                )
            # ponytail: TTSSpeakFrame is the canonical standalone-utterance signal for TTS
            # services (tts_service.py:752); TextFrame relies on sentence aggregator flush.
            await task.queue_frame(TTSSpeakFrame(text=_WELCOME_TEXT))

        # Do not block on_client_connected; transport needs to process on_app_message
        asyncio.create_task(_send_welcome())

    @transport.event_handler("on_client_disconnected")
    async def on_disconnected(_transport, _connection) -> None:
        # ponytail: pipecat's _on_client_disconnected only fires the event handler;
        # it does NOT push an EndFrame, so task.run() never returns. Cancel explicitly.
        # Session data is preserved — frontend owns lifecycle via DELETE /api/chat/sessions/{id}.
        _log.info("WebRTC transport disconnected for session %s — cancelling pipeline", session_id)
        await task.cancel()

    _log.info("Starting voice pipeline for session_id=%s, pc_id=%s", session_id, webrtc_connection.pc_id)
    try:
        await webrtc_connection.connect()
        await task.run(WorkerParams(loop=asyncio.get_running_loop()))
    finally:
        _log.info("Voice pipeline stopped for session_id=%s, pc_id=%s", session_id, webrtc_connection.pc_id)
