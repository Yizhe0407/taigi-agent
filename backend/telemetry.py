import logging
import os
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.metrics.view import ExplicitBucketHistogramAggregation, View
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Status, StatusCode

_AGENT_INSTRUMENTATION_NAME = "taigi_bus_agent.agent"
_PIPELINE_INSTRUMENTATION_NAME = "taigi_bus_agent.pipeline"
_PROVIDER_INSTRUMENTATION_NAME = "taigi_bus_agent.provider"
_DEPARTURES_INSTRUMENTATION_NAME = "taigi_bus_agent.departures"

# Recommended bucket boundaries from OTel HTTP semantic conventions spec.
# https://opentelemetry.io/docs/specs/semconv/http/http-metrics/
_DURATION_BOUNDARIES = [0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.25, 0.5, 0.75, 1, 2.5, 5, 7.5, 10]

# Byte-range boundaries for audio size histograms (1 KB → 25 MB).
# Default OTel buckets top out at 10 000, which is useless for MB-scale audio.
_BYTES_BOUNDARIES = [
    1_024,
    4_096,
    16_384,
    65_536,
    262_144,
    1_048_576,
    4_194_304,
    10_485_760,
    26_214_400,
]

# Singleton — set once by configure_telemetry(), read by get_telemetry().
_telemetry: "AgentTelemetry | None" = None

# Content-level capture (user input / prompt / LLM 回應 / tool result / 轉譯文字)。
# 預設開啟；設 TELEMETRY_CAPTURE_CONTENT=false 關閉。原始音訊 bytes 不收 —
# binary 塞 span attribute 是反模式，metadata（大小、長度）已由 metrics 涵蓋。
_CAPTURE_CONTENT_ENV = "TELEMETRY_CAPTURE_CONTENT"
_CONTENT_MAX_CHARS = 4_000


def _truncate_content(text: str, limit: int) -> str:
    return text if len(text) <= limit else f"{text[:limit]}…[truncated {len(text)} chars]"


def _clean_attributes(attributes: Mapping[str, Any] | None) -> dict[str, Any]:
    if not attributes:
        return {}
    return {key: value for key, value in attributes.items() if value is not None}


def configure_telemetry(service_name: str = "taigi-bus-agent") -> "AgentTelemetry":
    """Set up OTLP/HTTP exporters and return the singleton AgentTelemetry.

    Idempotent — subsequent calls return the same instance.
    OTLP exporters are only registered when OTEL_EXPORTER_OTLP_ENDPOINT (or
    the per-signal variants) is set; otherwise the OTel SDK's default NoOp
    providers handle all spans and metrics silently.
    """
    global _telemetry

    if _telemetry is not None:
        return _telemetry

    if any(
        os.getenv(v)
        for v in (
            "OTEL_EXPORTER_OTLP_ENDPOINT",
            "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
            "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
        )
    ):
        resource = Resource.create({"service.name": os.getenv("OTEL_SERVICE_NAME", service_name)})

        tracer_provider = TracerProvider(resource=resource)
        tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        trace.set_tracer_provider(tracer_provider)

        # Wildcard covers auto-instrumented metrics (FastAPI/HTTPX) too.
        duration_view = View(
            instrument_name="*duration*",
            aggregation=ExplicitBucketHistogramAggregation(boundaries=_DURATION_BOUNDARIES),
        )
        # Default OTel buckets top out at 10 000 — useless for MB-scale audio.
        bytes_view = View(
            instrument_name="*bytes*",
            aggregation=ExplicitBucketHistogramAggregation(boundaries=_BYTES_BOUNDARIES),
        )
        metric_reader = PeriodicExportingMetricReader(OTLPMetricExporter())
        metrics.set_meter_provider(
            MeterProvider(
                resource=resource,
                metric_readers=[metric_reader],
                views=[duration_view, bytes_view],
            )
        )

        # Forwards stdlib `logging` records (the _log = logging.getLogger(__name__)
        # calls throughout providers/services/voice) to OTLP. Attached at the root
        # logger with level=NOTSET so it only sees what already passed each
        # logger's own effective level (WARNING by default — no basicConfig sets
        # one). ponytail: uvicorn's own "uvicorn.access"/"uvicorn.error" loggers
        # have propagate=False, so they never reach this handler — only this
        # app's own module loggers are exported. Add an explicit forwarder later
        # if access logs in SigNoz turn out to matter.
        logger_provider = LoggerProvider(resource=resource)
        logger_provider.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter()))
        logging.getLogger().addHandler(LoggingHandler(level=logging.NOTSET, logger_provider=logger_provider))

    _telemetry = AgentTelemetry()
    return _telemetry


def get_telemetry() -> "AgentTelemetry":
    """Return the singleton AgentTelemetry, calling configure_telemetry() if needed."""
    return _telemetry if _telemetry is not None else configure_telemetry()


class AgentTelemetry:
    """OpenTelemetry spans and metrics emitted by the agent harness and pipeline."""

    def __init__(self) -> None:
        self._capture_content = os.getenv(_CAPTURE_CONTENT_ENV, "true").strip().lower() not in ("false", "0", "no")

        # ── Agent instrumentation (LLM + tool calls) ─────────────────────────
        agent_meter = metrics.get_meter(_AGENT_INSTRUMENTATION_NAME)
        self._tracer = trace.get_tracer(_AGENT_INSTRUMENTATION_NAME)
        self._llm_duration = agent_meter.create_histogram(
            "agent.llm.duration",
            unit="s",
            description="Duration of one LLM request attempt.",
        )
        self._llm_retries = agent_meter.create_counter(
            "agent.llm.retry",
            unit="{retry}",
            description="LLM retry attempts emitted by the harness.",
        )
        self._tool_duration = agent_meter.create_histogram(
            "agent.tool.duration",
            unit="s",
            description="Duration of one dispatched tool call.",
        )
        self._tool_errors = agent_meter.create_counter(
            "agent.tool.error",
            unit="{error}",
            description="Tool dispatch and handler failures.",
        )
        self._turns = agent_meter.create_counter(
            "agent.turn",
            unit="{turn}",
            description=("Completed agent turns. Attributes: agent.router.path (canned/llm), agent.router.intent, agent.outcome (ok/tool_round_limit/error)."),
        )
        self._turn_tool_rounds = agent_meter.create_histogram(
            "agent.turn.tool_rounds",
            unit="{round}",
            description="Tool-call rounds consumed by one LLM-path turn.",
        )

        # ── Provider instrumentation (upstream caches) ────────────────────────
        provider_meter = metrics.get_meter(_PROVIDER_INSTRUMENTATION_NAME)
        self._cache_lookups = provider_meter.create_counter(
            "provider.cache.lookup",
            unit="{lookup}",
            description=("Upstream-data cache lookups. Attributes: cache.name (e.g. tdx.route_info), cache.outcome (hit/miss)."),
        )
        self._provider_fallbacks = provider_meter.create_counter(
            "provider.fallback",
            unit="{call}",
            description=(
                "HybridBusProvider ebus→TDX fallback outcomes. Attributes: "
                "provider.operation (eta/route_estimate), provider.outcome "
                "(ebus_hit/tdx_fallback/both_empty)."
            ),
        )

        # ── Departures instrumentation (decision classification) ──────────────
        departures_meter = metrics.get_meter(_DEPARTURES_INSTRUMENTATION_NAME)
        self._departure_decisions = departures_meter.create_counter(
            "departures.decision",
            unit="{decision}",
            description=(
                "Departure decisions produced by _classify_stop, plus rows dropped "
                "by the terminal-direction auto-filter. Attribute: departures.decision "
                "(arriving_soon/can_wait/long_wait/not_departed/last_departed/unknown/"
                "filtered_terminal_direction)."
            ),
        )

        # ── Pipeline instrumentation (ASR / TTS stages) ───────────────────────
        pipeline_meter = metrics.get_meter(_PIPELINE_INSTRUMENTATION_NAME)
        self._pipeline_stage_duration = pipeline_meter.create_histogram(
            "pipeline.stage.duration",
            unit="s",
            description=("Duration of a pipeline processing stage. Attributes: pipeline.stage (e.g. tts.text_process), pipeline.outcome."),
        )
        self._asr_audio_bytes = pipeline_meter.create_histogram(
            "pipeline.asr.audio_bytes",
            unit="By",
            description="Size of audio uploaded to the ASR proxy endpoint.",
        )
        self._tts_audio_bytes = pipeline_meter.create_histogram(
            "pipeline.tts.audio_bytes",
            unit="By",
            description="Size of the concatenated WAV returned by the TTS endpoint.",
        )

        # ── Voice (pipecat WebRTC pipeline) instrumentation ───────────────────
        self._voice_sessions = pipeline_meter.create_counter(
            "pipeline.voice.session",
            unit="{session}",
            description="WebRTC voice session connect/disconnect events. Attributes: pipeline.outcome (connected/disconnected).",
        )
        self._voice_active_sessions = pipeline_meter.create_up_down_counter(
            "pipeline.voice.active_sessions",
            unit="{session}",
            description="Number of currently active WebRTC voice sessions.",
        )
        self._voice_barge_in = pipeline_meter.create_counter(
            "pipeline.voice.barge_in",
            unit="{interruption}",
            description="Times the user interrupted (barge-in) the bot while it was speaking.",
        )
        self._voice_turn_duration = pipeline_meter.create_histogram(
            "pipeline.voice.turn.duration",
            unit="s",
            description=("Latency from user utterance transcription (STT output) to the first TTS audio frame produced for the reply."),
        )

    # ── Span helpers ──────────────────────────────────────────────────────────

    @contextmanager
    def start_span(self, name: str, attributes: Mapping[str, Any] | None = None) -> Iterator[Any]:
        with self._tracer.start_as_current_span(name, attributes=_clean_attributes(attributes)) as span:
            yield span

    def mark_span_error(
        self,
        span: Any,
        *,
        error_type: str,
        exception: Exception | None = None,
        description: str | None = None,
    ) -> None:
        span.set_attribute("error.type", error_type)
        if exception is not None:
            span.record_exception(exception)
        span.set_status(Status(StatusCode.ERROR, description or error_type))

    # ── Content capture ───────────────────────────────────────────────────────

    @property
    def capture_content(self) -> bool:
        return self._capture_content

    def set_content(
        self,
        span: Any,
        key: str,
        text: object,
        *,
        limit: int = _CONTENT_MAX_CHARS,
    ) -> None:
        """Attach content-level text to a span, truncated to `limit` chars.

        No-op when TELEMETRY_CAPTURE_CONTENT=false or text is None/empty.
        Never raises — telemetry must not break the request path.
        """
        if not self._capture_content or text is None:
            return
        try:
            value = text if isinstance(text, str) else str(text)
            if value:
                span.set_attribute(key, _truncate_content(value, limit))
        except Exception:  # noqa: BLE001 — content capture is best-effort
            pass

    def set_current_content(
        self,
        key: str,
        text: object,
        *,
        limit: int = _CONTENT_MAX_CHARS,
    ) -> None:
        """`set_content` onto the current span (e.g. the FastAPI request span)."""
        span = trace.get_current_span()
        if span.is_recording():
            self.set_content(span, key, text, limit=limit)

    # ── Agent metrics ─────────────────────────────────────────────────────────

    def record_llm_duration(self, duration_s: float, *, model: str, operation: str, outcome: str) -> None:
        self._llm_duration.record(
            duration_s,
            {
                "agent.llm.model": model,
                "agent.llm.operation": operation,
                "agent.outcome": outcome,
            },
        )

    def record_llm_retry(self, *, operation: str, error_type: str) -> None:
        self._llm_retries.add(
            1,
            {
                "agent.llm.operation": operation,
                "error.type": error_type,
            },
        )

    def trace_tool_routing(self, tool_names: Sequence[str], *, accepted: bool) -> None:
        trace.get_current_span().add_event(
            "agent.tool.routing",
            {
                "agent.tool.count": len(tool_names),
                "agent.tool.names": ",".join(tool_names),
                "agent.tool.accepted": accepted,
            },
        )

    def record_tool_duration(self, duration_s: float, *, tool_name: str, outcome: str) -> None:
        self._tool_duration.record(
            duration_s,
            {
                "agent.tool.name": tool_name,
                "agent.outcome": outcome,
            },
        )

    def record_tool_error(self, *, tool_name: str, error_type: str) -> None:
        self._tool_errors.add(
            1,
            {
                "agent.tool.name": tool_name,
                "error.type": error_type,
            },
        )

    def record_turn(
        self,
        *,
        path: str,
        intent: str,
        outcome: str,
        tool_rounds: int | None = None,
    ) -> None:
        """Record one completed agent turn.

        `tool_rounds` is only meaningful on the LLM path; canned turns pass None.
        """
        self._turns.add(
            1,
            {
                "agent.router.path": path,
                "agent.router.intent": intent,
                "agent.outcome": outcome,
            },
        )
        if tool_rounds is not None:
            self._turn_tool_rounds.record(tool_rounds, {"agent.outcome": outcome})

    # ── Provider metrics ──────────────────────────────────────────────────────

    def record_cache_lookup(self, *, cache: str, hit: bool) -> None:
        """Record an upstream-data cache lookup (hit ratio drives TTL tuning)."""
        self._cache_lookups.add(
            1,
            {
                "cache.name": cache,
                "cache.outcome": "hit" if hit else "miss",
            },
        )

    def record_provider_fallback(self, *, operation: str, outcome: str) -> None:
        """Record a HybridBusProvider ebus→TDX fallback decision.

        `operation` identifies the call site (eta/route_estimate); `outcome`
        is ebus_hit/tdx_fallback/both_empty. A silent ebus outage shows up as
        a rising tdx_fallback (and eventually both_empty) rate here.
        """
        self._provider_fallbacks.add(
            1,
            {
                "provider.operation": operation,
                "provider.outcome": outcome,
            },
        )

    # ── Departures metrics ─────────────────────────────────────────────────────

    def record_departure_decision(self, *, decision: str) -> None:
        """Record one departure decision classification (or a terminal-direction filter drop)."""
        self._departure_decisions.add(1, {"departures.decision": decision})

    # ── Pipeline metrics ──────────────────────────────────────────────────────

    def record_pipeline_stage(self, duration_s: float, *, stage: str, outcome: str) -> None:
        """Record latency for a named pipeline processing stage.

        Args:
            duration_s: Wall-clock seconds measured with time.perf_counter().
            stage:      Dot-separated stage identifier, e.g. ``"tts.text_process"``.
            outcome:    ``"ok"`` or ``"error"``.
        """
        self._pipeline_stage_duration.record(
            duration_s,
            {
                "pipeline.stage": stage,
                "pipeline.outcome": outcome,
            },
        )

    def record_asr_audio_bytes(self, n_bytes: int) -> None:
        """Record size of an audio upload to the ASR proxy."""
        self._asr_audio_bytes.record(n_bytes)

    def record_tts_audio_bytes(self, n_bytes: int) -> None:
        """Record size of the synthesized WAV returned to the frontend."""
        self._tts_audio_bytes.record(n_bytes)

    def record_voice_session(self, *, outcome: str) -> None:
        """Record a WebRTC voice session connect/disconnect event."""
        self._voice_sessions.add(1, {"pipeline.outcome": outcome})

    def record_voice_active_sessions(self, delta: int) -> None:
        """Adjust the currently-active WebRTC voice session gauge (+1 connect, -1 disconnect)."""
        self._voice_active_sessions.add(delta)

    def record_voice_barge_in(self) -> None:
        """Record one user barge-in (interrupting the bot mid-speech)."""
        self._voice_barge_in.add(1)

    def record_voice_turn_latency(self, duration_s: float) -> None:
        """Record latency from STT transcription to the first TTS audio frame."""
        self._voice_turn_duration.record(duration_s)
