"""Content-capture behaviour of AgentTelemetry.set_content."""

from telemetry import AgentTelemetry


class FakeSpan:
    def __init__(self):
        self.attributes = {}

    def set_attribute(self, key, value):
        self.attributes[key] = value


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
