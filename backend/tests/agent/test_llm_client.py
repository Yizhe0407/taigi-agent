"""Regression tests for `call_llm_stream`'s blocked/delta_emitted short-circuit.

Once a content delta has been yielded to the caller (already spoken by TTS or
shown in an SSE frame), a retryable error later in the same stream must not
trigger a retry — replaying the HTTP call would re-yield content the caller
already emitted. See `agent.llm_client._handle_llm_attempt_error`'s `blocked`
param and `call_llm_stream`'s `delta_emitted` flag.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from types import SimpleNamespace

import httpx
from openai import APIConnectionError

from agent.llm_client import call_llm_stream


def _delta_chunk(content: str):
    return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=content, tool_calls=None))])


async def _stream_then_raise(contents: list[str], error: Exception):
    """Fake `create(stream=True)` return value: yield content deltas, then raise."""
    for content in contents:
        yield _delta_chunk(content)
    raise error


class FakeCompletions:
    def __init__(self, stream):
        self._stream = stream
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._stream


class FakeClient:
    def __init__(self, stream):
        self.chat = SimpleNamespace(completions=FakeCompletions(stream))


class NoOpTelemetry:
    """Minimal telemetry double — this test only cares about retry/yield behavior."""

    @contextmanager
    def start_span(self, name, attributes=None):
        yield SimpleNamespace(name=name)

    def set_content(self, span, key, text, *, limit=None):
        pass

    def mark_span_error(self, span, *, error_type, exception=None, description=None):
        pass

    def record_llm_duration(self, duration_s, *, model, operation, outcome):
        pass

    def record_llm_retry(self, *, operation, error_type):
        raise AssertionError("must not retry once a delta has already been yielded")


def _connection_error() -> APIConnectionError:
    request = httpx.Request("POST", "http://localhost/v1/chat/completions")
    return APIConnectionError(request=request)


def test_call_llm_stream_does_not_retry_after_delta_already_emitted():
    """Retryable error after >=1 delta yielded: no retry, error propagates, no duplicate deltas."""
    error = _connection_error()
    stream = _stream_then_raise(["先講", "一半"], error)
    client = FakeClient(stream)
    telemetry = NoOpTelemetry()

    async def run():
        events = []
        try:
            async for kind, value in call_llm_stream(
                client,
                "test-model",
                [{"role": "user", "content": "hi"}],
                None,
                {},
                telemetry,
                operation="respond",
            ):
                events.append((kind, value))
        except APIConnectionError as raised:
            return events, raised
        raise AssertionError("expected APIConnectionError to propagate, not be swallowed/retried")

    events, raised = asyncio.run(run())

    # 1) Exactly one HTTP attempt — no retry after a delta was already yielded.
    assert len(client.chat.completions.calls) == 1
    # 2) The original retryable error propagates unchanged (bare `raise` reraise
    #    inside `_handle_llm_attempt_error`'s `blocked=True` path).
    assert raised is error
    # 3) Already-yielded deltas appear exactly once each — no replay/duplication.
    assert events == [("delta", "先講"), ("delta", "一半")]
