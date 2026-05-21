import os
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Status, StatusCode

_INSTRUMENTATION_NAME = "taigi_bus_agent.agent"
_OTLP_ENDPOINT_ENV_VARS = (
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
    "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
)
_configured = False


def _clean_attributes(attributes: Mapping[str, Any] | None) -> dict[str, Any]:
    if not attributes:
        return {}
    return {key: value for key, value in attributes.items() if value is not None}


def _has_otlp_endpoint() -> bool:
    return any(os.getenv(name) for name in _OTLP_ENDPOINT_ENV_VARS)


def configure_telemetry(service_name: str = "taigi-bus-agent") -> "AgentTelemetry":
    """Enable OTLP/HTTP exporters when the deployment provides an endpoint."""
    global _configured

    if _configured or not _has_otlp_endpoint():
        return AgentTelemetry()

    resource = Resource.create(
        {"service.name": os.getenv("OTEL_SERVICE_NAME", service_name)}
    )

    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(tracer_provider)

    metric_reader = PeriodicExportingMetricReader(OTLPMetricExporter())
    metrics.set_meter_provider(
        MeterProvider(resource=resource, metric_readers=[metric_reader])
    )

    _configured = True
    return AgentTelemetry()


class AgentTelemetry:
    """OpenTelemetry spans and metrics emitted by the agent harness."""

    def __init__(self) -> None:
        self._tracer = trace.get_tracer(_INSTRUMENTATION_NAME)
        meter = metrics.get_meter(_INSTRUMENTATION_NAME)
        self._llm_duration = meter.create_histogram(
            "agent.llm.duration",
            unit="s",
            description="Duration of one LLM request attempt.",
        )
        self._llm_retries = meter.create_counter(
            "agent.llm.retry",
            unit="{retry}",
            description="LLM retry attempts emitted by the harness.",
        )
        self._tool_duration = meter.create_histogram(
            "agent.tool.duration",
            unit="s",
            description="Duration of one dispatched tool call.",
        )
        self._tool_errors = meter.create_counter(
            "agent.tool.error",
            unit="{error}",
            description="Tool dispatch and handler failures.",
        )

    @contextmanager
    def start_span(
        self, name: str, attributes: Mapping[str, Any] | None = None
    ) -> Iterator[Any]:
        with self._tracer.start_as_current_span(
            name, attributes=_clean_attributes(attributes)
        ) as span:
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

    def record_llm_duration(
        self, duration_s: float, *, model: str, operation: str, outcome: str
    ) -> None:
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
        with self.start_span(
            "agent.tool.routing",
            {
                "agent.tool.count": len(tool_names),
                "agent.tool.names": ",".join(tool_names),
                "agent.tool.accepted": accepted,
            },
        ):
            pass

    def record_tool_duration(
        self, duration_s: float, *, tool_name: str, outcome: str
    ) -> None:
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
