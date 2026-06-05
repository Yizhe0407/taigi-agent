import os
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.metrics.view import ExplicitBucketHistogramAggregation, View
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Status, StatusCode

_AGENT_INSTRUMENTATION_NAME = "taigi_bus_agent.agent"
_PIPELINE_INSTRUMENTATION_NAME = "taigi_bus_agent.pipeline"

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

    _telemetry = AgentTelemetry()
    return _telemetry


def get_telemetry() -> "AgentTelemetry":
    """Return the singleton AgentTelemetry, calling configure_telemetry() if needed."""
    return _telemetry if _telemetry is not None else configure_telemetry()


class AgentTelemetry:
    """OpenTelemetry spans and metrics emitted by the agent harness and pipeline."""

    def __init__(self) -> None:
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
