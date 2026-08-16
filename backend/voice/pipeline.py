"""Pipecat voice pipeline assembly."""

import asyncio
import json
import logging
import time
from collections import deque
from collections.abc import Callable
from contextlib import suppress
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

    Single mutable timestamp slot shared by both processors, so a barge-in
    that starts a new turn mid-measurement can produce a stale/dropped sample.
    Low-stakes metric — fine for now, upgrade to per-turn IDs if it gets noisy.
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

    deque.clear() would leave in-flight futures un-resolved and their awaiters
    hanging forever, so pop each entry and resolve its future before discarding
    it. Only the last chunk of each add_audio_bytes() call carries a future.
    """
    while queue:
        _, fut = queue.popleft()
        if fut is not None and not fut.done():
            fut.set_result(True)


class BargeInProcessor(FrameProcessor):
    """Convert VADUserStartedSpeakingFrame → broadcast_interruption(), gated on bot speaking.

    PipelineWorker's LLMResponseUniversalAggregator normally does this, but we
    use a custom agent instead of an LLMService, so we need it explicitly.

    Gated on bot-speaking: while the bot is silent (still reasoning/tool-calling,
    nothing pushed downstream yet), a VAD blip from noise/cough would kill
    in-flight work with no recovery path, so this processor stays quiet in that
    case. The barge-in-before-audio-starts race is instead closed at the
    inference level in agent_processor.py, which cancels the prior task on the
    next transcript and sends its own InterruptionFrame if that task had
    already pushed frames downstream.

    Also forwards bot-speaking / user-speaking state to the client over the data
    channel (send_event) so the frontend can drive its playback and listening/
    recognizing UI states. user_speaking fires regardless of the bot-speaking
    gate; only the interruption itself stays gated on the bot currently talking.
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

    Placed after transport.output(): BaseOutputTransport's _audio_task_handler
    drains its audio queue at real playback speed and only pushes a frame
    downstream once it's actually queued for playback (a pts-less frame like
    SubtitleFrame queues inline with audio — see tts_taigi.py). So a processor
    here sees each SubtitleFrame at ~the moment its audio starts playing, not
    at LLM-generation time, letting the frontend reveal durationMs of text
    progressively over the real playback window instead of dumping it at once.
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

    pipecat's handle_interruptions() resets its own _audio_queue, but audio
    already written to aiortc's RawAudioTrack._chunk_queue keeps playing —
    there's no public API to clear it, so override until upstream adds one.
    """

    async def _handle_frame(self, frame: Frame) -> None:
        if isinstance(frame, InterruptionFrame):
            if self._client and getattr(self._client, "_audio_output_track", None):
                track = self._client._audio_output_track
                chunk_queue = getattr(track, "_chunk_queue", None)
                # Guard the type (aiortc uses a plain deque here) so a future
                # aiortc refactor doesn't silently no-op instead of erroring.
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

    # on_disconnected already cancels the worker, so PipelineWorker's own 5-min
    # idle kill is unnecessary and would just cut off a still-live connection.
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
    # Strong reference to the fire-and-forget welcome task — asyncio only holds
    # a weak reference to a task once nothing else does, so without this the
    # task risks being garbage-collected mid-run. Cleared via done_callback.
    _welcome_task: asyncio.Task | None = None

    @transport.event_handler("on_app_message")
    async def on_app_message(_transport, message, _sender) -> None:
        try:
            data = json.loads(message) if isinstance(message, (str, bytes)) else message
            if isinstance(data, dict) and data.get("type") == "client_ready":
                _log.debug("client_ready received for session %s", session_id)
                _client_ready.set()
        except Exception as exc:
            _log.debug("Failed to process app message for session %s: %s", session_id, exc)

    @transport.event_handler("on_client_connected")
    async def on_connected(_transport, _connection) -> None:
        nonlocal _session_active, _welcome_task
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
            # Announce the text at the same moment we queue the audio, so the
            # subtitle can't appear seconds ahead of the voice.
            webrtc_connection.send_app_message({"type": "agent_reply", "text": _WELCOME_TEXT, "role": "assistant"})
            # TTSSpeakFrame is TTSService's canonical standalone-utterance signal;
            # a plain TextFrame would rely on the sentence aggregator to flush it.
            await task.queue_frame(TTSSpeakFrame(text=_WELCOME_TEXT))

        def _on_welcome_done(t: asyncio.Task) -> None:
            nonlocal _welcome_task
            _welcome_task = None
            if t.cancelled():
                return
            exc = t.exception()
            if exc:
                _log.exception("Welcome greeting task failed for session %s", session_id, exc_info=exc)

        # Do not block on_client_connected; transport needs to process on_app_message.
        _welcome_task = asyncio.create_task(_send_welcome())
        _welcome_task.add_done_callback(_on_welcome_done)

    @transport.event_handler("on_client_disconnected")
    async def on_disconnected(_transport, _connection) -> None:
        # pipecat's _on_client_disconnected only fires the event handler — it does
        # NOT push an EndFrame, so task.run() never returns on its own; cancel it.
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

        # The welcome task is fire-and-forget and can still be parked on the 3 s
        # _client_ready wait (or on queue_frame) when the pipeline unwinds; left
        # alone it would outlive run_voice_pipeline and keep the worker +
        # connection reachable, so cancel it and wait for its cleanup here.
        _pending_welcome = _welcome_task
        if _pending_welcome is not None and not _pending_welcome.done():
            _pending_welcome.cancel()
            with suppress(asyncio.CancelledError):
                await _pending_welcome

        # pipecat's SmallWebRTCRequestHandler only drops a pc from its _pcs_map
        # when the pc emits "closed", which only pc.close() triggers. The normal
        # client-disconnect path gets that from the client's own close; when
        # connect()/task.run() raises instead, nothing does, so the pc (and the
        # ICE/DTLS transports + Silero ONNX session hanging off it) would sit in
        # _pcs_map for the life of the process. disconnect() is idempotent
        # (aiortc's RTCPeerConnection.close() returns early once closed), so
        # this call is a no-op on the normal path and only matters here.
        try:
            await webrtc_connection.disconnect()
        except Exception as close_exc:
            # Never let a teardown failure replace the original exception —
            # log it and let the in-flight exception (if any) keep propagating.
            _log.warning("Failed to close WebRTC connection for session %s: %s", session_id, close_exc)

        _log.info("Voice pipeline stopped for session_id=%s, pc_id=%s", session_id, webrtc_connection.pc_id)
