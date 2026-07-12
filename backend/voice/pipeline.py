"""Pipecat voice pipeline assembly."""

import asyncio
import json
import logging
import time
from collections import deque
from collections.abc import Callable
from typing import Any

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    InterruptionFrame,
    TTSSpeakFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
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

from agent.diagnostics import log_diagnostic
from telemetry import get_telemetry
from voice.agent_processor import TaigiBusAgentProcessor
from voice.stt_breeze import BreezeSTTService
from voice.tts_taigi import SubtitleFrame, TaigiTTSService

_log = logging.getLogger(__name__)

_WELCOME_TEXT = "請問您欲前往哪裡？"


class TurnLatencyTracker:
    """Bridges 'user finished speaking -> first TTS audio frame' latency across
    two separate pipeline stages (agent_processor -> tts_taigi).

    ponytail: single mutable timestamp slot shared by both processors for one
    pipeline run. Correct for the common non-overlapping-turn case; a barge-in
    that starts a new turn mid-measurement can produce a stale/dropped sample
    (low-stakes latency metric — upgrade to per-turn IDs if it gets noisy).
    """

    def __init__(self) -> None:
        self._t0: float | None = None

    def mark_transcription(self) -> None:
        self._t0 = time.perf_counter()

    def mark_first_audio(self) -> None:
        if self._t0 is None:
            return
        get_telemetry().record_voice_turn_latency(time.perf_counter() - self._t0)
        self._t0 = None


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

    Also forwards these two frames to the client over the data channel (send_event)
    as {"type": "bot_speaking"} / {"type": "bot_silent"} — the frontend uses this to
    know playback is still active instead of relying solely on timers.

    Likewise forwards the Silero VAD frames as {"type": "user_speaking"} /
    {"type": "user_silent"} so the frontend's conversation state machine can show
    a "listening" vs "recognizing" phase. user_speaking fires regardless of the
    bot-speaking gate (the frontend always wants to know the mic picked up speech);
    only the barge-in interruption stays gated on the bot currently talking.
    """

    def __init__(self, send_event: Callable[[Any], None] | None = None, **kwargs):
        super().__init__(**kwargs)
        self._bot_speaking = False
        self._send_event = send_event

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, BotStartedSpeakingFrame):
            self._bot_speaking = True
            if self._send_event:
                self._send_event({"type": "bot_speaking"})
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._bot_speaking = False
            if self._send_event:
                self._send_event({"type": "bot_silent"})
        elif isinstance(frame, VADUserStartedSpeakingFrame):
            if self._send_event:
                self._send_event({"type": "user_speaking"})
            if self._bot_speaking:
                _log.debug("Barge-in detected while bot speaking, broadcasting interruption")
                get_telemetry().record_voice_barge_in()
                await self.broadcast_interruption()
        elif isinstance(frame, VADUserStoppedSpeakingFrame):
            if self._send_event:
                self._send_event({"type": "user_silent"})
        await self.push_frame(frame, direction)


class SubtitleSyncProcessor(FrameProcessor):
    """Forwards SubtitleFrames to the client as playback-synced subtitles.

    Placed after transport.output() in the pipeline: BaseOutputTransport's
    _audio_task_handler drains its internal audio queue at real playback
    speed and only pushes each frame downstream once it has actually been
    queued for playback (frames with no `pts` queue inline with audio — see
    SubtitleFrame's docstring in tts_taigi.py). A processor sitting after
    transport.output() therefore sees each SubtitleFrame at ~the moment its
    audio starts playing, not at LLM-generation time. The frontend uses
    durationMs to reveal the text progressively over the actual playback
    window instead of dumping it all at once.
    """

    def __init__(self, send_event: Callable[[Any], None] | None = None, **kwargs):
        super().__init__(**kwargs)
        self._send_event = send_event

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, SubtitleFrame) and self._send_event:
            self._send_event({"type": "subtitle", "text": frame.text, "durationMs": frame.duration_ms})
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
    barge_in = BargeInProcessor(send_event=webrtc_connection.send_app_message)
    stt = BreezeSTTService()
    turn_timer = TurnLatencyTracker()
    agent = TaigiBusAgentProcessor(
        session_id=session_id,
        send_event=webrtc_connection.send_app_message,
        turn_timer=turn_timer,
    )
    tts = TaigiTTSService(turn_timer=turn_timer)
    subtitle_sync = SubtitleSyncProcessor(send_event=webrtc_connection.send_app_message)

    pipeline = Pipeline(
        processors=[
            transport.input(),
            vad,
            barge_in,
            stt,
            agent,
            tts,
            transport.output(),
            subtitle_sync,
        ]
    )

    # ponytail: idle_timeout_secs=None — disconnect cancels the worker (fix #1),
    # so the 5-min idle kill is unnecessary and harmful (kills a live connection).
    task = PipelineWorker(pipeline, idle_timeout_secs=None, params=PipelineParams())

    # Event set by on_app_message when the client sends {"type": "client_ready"}.
    # The welcome greeting is held until the frontend confirms its audio element is playing,
    # eliminating the race between server-side TTS output and browser-side audio track setup.
    _client_ready = asyncio.Event()
    # Tracks whether record_voice_active_sessions(+1) has an outstanding -1 owed.
    # on_client_disconnected normally pays it back; the outer finally below
    # covers crashes/early-exit paths that skip that event entirely, so the
    # gauge can't leak a permanent +1 per abnormal session end.
    _session_active = False

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
        nonlocal _session_active
        _session_active = True
        get_telemetry().record_voice_session(outcome="connected")
        get_telemetry().record_voice_active_sessions(1)

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
            # Single source of truth for the welcome: the server announces the text on
            # the data channel (subtitle/chat) at the same moment it queues the audio,
            # so the subtitle can no longer appear seconds before the voice.
            webrtc_connection.send_app_message({"type": "agent_reply", "text": _WELCOME_TEXT, "role": "assistant"})
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
        nonlocal _session_active
        _session_active = False
        _log.info("WebRTC transport disconnected for session %s — cancelling pipeline", session_id)
        log_diagnostic("voice.pipeline", f"session={session_id} disconnected, cancelling pipeline")
        get_telemetry().record_voice_session(outcome="disconnected")
        get_telemetry().record_voice_active_sessions(-1)
        await task.cancel()

    _log.info("Starting voice pipeline for session_id=%s, pc_id=%s", session_id, webrtc_connection.pc_id)
    try:
        await webrtc_connection.connect()
        await task.run(WorkerParams(loop=asyncio.get_running_loop()))
    except Exception as exc:
        log_diagnostic("voice.pipeline", f"session={session_id} pipeline crashed: {exc}")
        raise
    finally:
        if _session_active:
            # Crashed/exited without on_client_disconnected firing — pay back
            # the +1 recorded in on_connected so the gauge doesn't drift.
            get_telemetry().record_voice_active_sessions(-1)
        _log.info("Voice pipeline stopped for session_id=%s, pc_id=%s", session_id, webrtc_connection.pc_id)
