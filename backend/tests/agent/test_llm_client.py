"""Regression tests for LLM HTTP request ownership and stream retry boundaries.

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
from typing import cast

import httpx
import pytest
from openai import APIConnectionError

from agent.llm_client import call_llm, call_llm_stream
from async_lifecycle import AsyncResourceOwner
from telemetry import AgentTelemetry


def _delta_chunk(content: str):
    return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=content, tool_calls=None))])


class CloseTrackingStream:
    """OpenAI AsyncStream-shaped fake with one asynchronous ``close`` owner."""

    def __init__(
        self,
        chunks,
        *,
        terminal_error: Exception | None = None,
        close_errors: list[Exception] | None = None,
    ):
        self._chunks = iter(chunks)
        self._terminal_error = terminal_error
        self._terminal_error_raised = False
        self._close_errors = iter(close_errors or [])
        self.close_calls = 0
        self.close_successes = 0

    @property
    def closed(self) -> bool:
        return self.close_successes > 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._chunks)
        except StopIteration:
            if self._terminal_error is not None and not self._terminal_error_raised:
                self._terminal_error_raised = True
                raise self._terminal_error
            raise StopAsyncIteration from None

    async def close(self):
        self.close_calls += 1
        error = next(self._close_errors, None)
        if error is not None:
            raise error
        self.close_successes += 1


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


class ControlledCompletions:
    def __init__(self, response, *, error: Exception | None = None):
        self.response = response
        self.error = error
        self.calls: list[dict] = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        self.started.set()
        await self.release.wait()
        if self.error is not None:
            raise self.error
        return self.response


def _client_with(completions):
    return SimpleNamespace(chat=SimpleNamespace(completions=completions))


def _completion_response(content: str = "ok"):
    message = SimpleNamespace(content=content, tool_calls=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


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


def _telemetry() -> AgentTelemetry:
    return cast(AgentTelemetry, NoOpTelemetry())


def _connection_error() -> APIConnectionError:
    request = httpx.Request("POST", "http://localhost/v1/chat/completions")
    return APIConnectionError(request=request)


def test_call_llm_holds_request_lease_until_non_streaming_create_settles():
    async def run() -> None:
        response = _completion_response()
        completions = ControlledCompletions(response)
        owner = AsyncResourceOwner("test LLM HTTP requests")
        request = asyncio.create_task(
            call_llm(
                _client_with(completions),
                "test-model",
                [{"role": "user", "content": "hi"}],
                None,
                {},
                _telemetry(),
                http_owner=owner,
                operation="respond",
            )
        )

        await completions.started.wait()
        assert owner.pending_count == 1

        shutdown = asyncio.create_task(owner.aclose())
        await asyncio.sleep(0)
        assert owner.closing
        assert not shutdown.done()

        completions.release.set()
        assert await request is response
        await shutdown

        assert owner.pending_count == 0
        assert owner.closed
        assert len(completions.calls) == 1

    asyncio.run(run())


def test_call_llm_closed_generation_never_reaches_upstream():
    async def run() -> None:
        completions = ControlledCompletions(_completion_response())
        owner = AsyncResourceOwner("test LLM HTTP requests")
        owner.close_admission()

        with pytest.raises(RuntimeError, match="owner is closing"):
            await call_llm(
                _client_with(completions),
                "test-model",
                [{"role": "user", "content": "hi"}],
                None,
                {},
                _telemetry(),
                http_owner=owner,
                operation="respond",
            )

        assert completions.calls == []
        await owner.aclose()

    asyncio.run(run())


def test_call_llm_error_aborts_request_lease_without_cleanup_debt():
    async def run() -> None:
        error = ValueError("request failed")
        completions = ControlledCompletions(_completion_response(), error=error)
        completions.release.set()
        owner = AsyncResourceOwner("test LLM HTTP requests")

        with pytest.raises(ValueError, match="request failed") as raised:
            await call_llm(
                _client_with(completions),
                "test-model",
                [{"role": "user", "content": "hi"}],
                None,
                {},
                _telemetry(),
                http_owner=owner,
                operation="respond",
            )

        assert raised.value is error
        assert owner.pending_count == 0
        assert owner.owned_count == 0
        await owner.aclose()

    asyncio.run(run())


def test_call_llm_cancellation_aborts_request_lease_without_detaching_constructor():
    async def run() -> None:
        completions = ControlledCompletions(_completion_response())
        owner = AsyncResourceOwner("test LLM HTTP requests")
        request = asyncio.create_task(
            call_llm(
                _client_with(completions),
                "test-model",
                [{"role": "user", "content": "hi"}],
                None,
                {},
                _telemetry(),
                http_owner=owner,
                operation="respond",
            )
        )

        await completions.started.wait()
        assert owner.pending_count == 1
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request

        assert owner.pending_count == 0
        assert owner.owned_count == 0
        await owner.aclose()

    asyncio.run(run())


def test_call_llm_stream_does_not_retry_after_delta_already_emitted():
    """Retryable error after >=1 delta yielded: no retry, error propagates, no duplicate deltas."""
    error = _connection_error()
    stream = CloseTrackingStream(
        [_delta_chunk("先講"), _delta_chunk("一半")],
        terminal_error=error,
    )
    client = FakeClient(stream)
    telemetry = _telemetry()
    owner = AsyncResourceOwner("test LLM streams")

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
                http_owner=owner,
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
    assert stream.close_calls == 1


def test_call_llm_stream_closes_upstream_when_consumer_disconnects():
    """Closing the outer generator at a yielded delta must close the upstream
    HTTP stream immediately instead of waiting for async-generator GC."""
    stream = CloseTrackingStream([_delta_chunk("第一段"), _delta_chunk("第二段")])
    client = FakeClient(stream)
    owner = AsyncResourceOwner("test LLM streams")

    async def run():
        events = call_llm_stream(
            client,
            "test-model",
            [{"role": "user", "content": "hi"}],
            None,
            {},
            _telemetry(),
            http_owner=owner,
            operation="respond",
        )
        assert await anext(events) == ("delta", "第一段")
        assert stream.closed is False
        await events.aclose()
        assert stream.closed is True

    asyncio.run(run())


def test_consumer_cancellation_cannot_interrupt_upstream_stream_close():
    """The outer-generator waiter remains cancelled, but only after the one
    physical HTTP-stream close operation has completed."""

    async def run():
        close_started = asyncio.Event()
        close_release = asyncio.Event()

        class _SlowCloseStream(CloseTrackingStream):
            def __init__(self):
                super().__init__([_delta_chunk("第一段"), _delta_chunk("第二段")])
                self.close_finished = False
                self.close_cancelled = False

            async def close(self):
                self.close_calls += 1
                close_started.set()
                try:
                    await close_release.wait()
                except asyncio.CancelledError:
                    self.close_cancelled = True
                    raise
                self.close_finished = True

        stream = _SlowCloseStream()
        owner = AsyncResourceOwner("test LLM streams")
        events = call_llm_stream(
            FakeClient(stream),
            "test-model",
            [{"role": "user", "content": "hi"}],
            None,
            {},
            _telemetry(),
            http_owner=owner,
            operation="respond",
        )
        assert await anext(events) == ("delta", "第一段")

        closing = asyncio.create_task(events.aclose())
        await asyncio.wait_for(close_started.wait(), timeout=2.0)
        closing.cancel()
        await asyncio.sleep(0)

        assert not closing.done(), "outer generator closed before HTTP stream release"
        assert stream.close_cancelled is False

        close_release.set()
        with pytest.raises(asyncio.CancelledError):
            await closing

        assert stream.close_calls == 1
        assert stream.close_finished is True
        assert stream.close_cancelled is False

    asyncio.run(run())


def test_close_failure_remains_owned_until_next_lifecycle_retry_succeeds():
    close_error = RuntimeError("close failed")
    stream = CloseTrackingStream(
        [_delta_chunk("完整回覆")],
        close_errors=[close_error],
    )
    owner = AsyncResourceOwner("test LLM streams")

    async def run():
        events = call_llm_stream(
            FakeClient(stream),
            "test-model",
            [{"role": "user", "content": "hi"}],
            None,
            {},
            _telemetry(),
            http_owner=owner,
            operation="respond",
        )
        with pytest.raises(RuntimeError, match="close failed") as raised:
            async for _ in events:
                pass
        assert raised.value is close_error
        assert owner.owned_count == 1
        assert owner.failed_count == 1
        assert stream.close_calls == 1

        await owner.retry_failed()
        assert owner.owned_count == 0
        assert owner.failed_count == 0
        assert stream.close_calls == 2

        # A successful release removes ownership, so later retry/shutdown paths
        # cannot invoke the same physical close again.
        await owner.retry_failed()
        await owner.aclose()
        assert stream.close_calls == 2

    asyncio.run(run())


def test_iteration_and_close_failures_are_both_preserved_and_retryable():
    iteration_error = RuntimeError("iteration failed")
    close_error = RuntimeError("close failed")
    stream = CloseTrackingStream(
        [],
        terminal_error=iteration_error,
        close_errors=[close_error],
    )
    owner = AsyncResourceOwner("test LLM streams")

    async def run():
        events = call_llm_stream(
            FakeClient(stream),
            "test-model",
            [{"role": "user", "content": "hi"}],
            None,
            {},
            _telemetry(),
            http_owner=owner,
            operation="respond",
        )
        with pytest.raises(ExceptionGroup) as raised:
            async for _ in events:
                pass

        assert raised.value.exceptions == (iteration_error, close_error)
        assert owner.owned_count == 1
        assert owner.failed_count == 1
        assert stream.close_calls == 1

        await owner.retry_failed()
        assert owner.owned_count == 0
        assert stream.close_calls == 2

    asyncio.run(run())


def test_successful_iteration_close_failure_is_not_retried_as_an_llm_attempt():
    close_error = _connection_error()
    stream = CloseTrackingStream(
        [_delta_chunk("完整")],
        close_errors=[close_error],
    )
    client = FakeClient(stream)
    owner = AsyncResourceOwner("test LLM streams")

    async def run() -> None:
        events = call_llm_stream(
            client,
            "test-model",
            [{"role": "user", "content": "hi"}],
            None,
            {},
            _telemetry(),
            http_owner=owner,
            operation="respond",
        )

        yielded = []
        with pytest.raises(APIConnectionError) as raised:
            async for event in events:
                yielded.append(event)

        assert raised.value is close_error
        assert yielded == [("delta", "完整")]
        assert len(client.chat.completions.calls) == 1
        assert stream.close_calls == 1
        assert owner.owned_count == 1
        assert owner.failed_count == 1

        await owner.retry_failed()

        assert stream.close_calls == 2
        assert stream.closed
        assert owner.owned_count == 0
        assert owner.failed_count == 0
        assert len(client.chat.completions.calls) == 1

    asyncio.run(run())
