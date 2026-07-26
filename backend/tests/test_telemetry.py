"""Configuration and content-capture behaviour of backend telemetry."""

import pytest

from telemetry import AgentTelemetry, _enabled_otlp_signals


class FakeSpan:
    def __init__(self):
        self.attributes = {}

    def set_attribute(self, key, value):
        self.attributes[key] = value


_OTLP_ENDPOINT_VARS = (
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
    "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
    "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT",
)


@pytest.mark.parametrize(
    ("variable", "expected"),
    [
        ("OTEL_EXPORTER_OTLP_ENDPOINT", (True, True, True)),
        ("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", (True, False, False)),
        ("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT", (False, True, False)),
        ("OTEL_EXPORTER_OTLP_LOGS_ENDPOINT", (False, False, True)),
    ],
)
def test_enabled_otlp_signals_respects_common_and_per_signal_endpoints(
    monkeypatch,
    variable,
    expected,
):
    for name in _OTLP_ENDPOINT_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(variable, "http://collector:4318")

    assert _enabled_otlp_signals() == expected


def test_enabled_otlp_signals_are_disabled_without_endpoints(monkeypatch):
    for name in _OTLP_ENDPOINT_VARS:
        monkeypatch.delenv(name, raising=False)

    assert _enabled_otlp_signals() == (False, False, False)


def test_set_content_attaches_truncated_text():
    telemetry = AgentTelemetry()
    span = FakeSpan()

    telemetry.set_content(span, "agent.input.text", "201 幾分到")
    assert span.attributes["agent.input.text"] == "201 幾分到"

    telemetry.set_content(span, "agent.tool.result", "x" * 5000, limit=100)
    captured = span.attributes["agent.tool.result"]
    assert captured.startswith("x" * 100)
    assert captured.endswith("…[truncated 5000 chars]")


def test_set_content_skips_none_and_empty():
    telemetry = AgentTelemetry()
    span = FakeSpan()

    telemetry.set_content(span, "a", None)
    telemetry.set_content(span, "b", "")
    assert span.attributes == {}


def test_set_content_disabled_by_env(monkeypatch):
    monkeypatch.setenv("TELEMETRY_CAPTURE_CONTENT", "false")
    telemetry = AgentTelemetry()
    span = FakeSpan()

    telemetry.set_content(span, "agent.input.text", "去斗六")
    assert span.attributes == {}
    assert telemetry.capture_content is False
