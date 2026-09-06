"""Tests for TaigiBusAgentProcessor error-path event emission."""

import asyncio
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from pipecat.frames.frames import InterruptionFrame, LLMFullResponseEndFrame, LLMFullResponseStartFrame, TranscriptionFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from api.session_store import SessionTombstonedError
from async_lifecycle import AsyncResourceOwner, cancel_and_join_task
from voice.agent_processor import TaigiBusAgentProcessor, _ResponseState

DIRECTION = FrameDirection.DOWNSTREAM


class _FakeProcessor(TaigiBusAgentProcessor):
    """Minimal subclass: skip FrameProcessor.__init__, stub push_frame."""

    def __init__(self, session_id, send_event):
        self.session_id = session_id
        self._send_event = send_event
        self._inference_task = None
        self._inference_state = None
        self._turn_timer = None
        self._response_streams = AsyncResourceOwner("test agent response streams")
        self._closing = False
        self._cleanup_task = None
        self._base_cleanup_complete = False
        self._cleanup_complete = False
        # process_frame()'s base-class call touches this before our TranscriptionFrame
        # branch runs; TaigiBusAgentProcessor.__init__ is skipped here so it's unset.
        self._observer = None

    async def push_frame(self, frame, direction=FrameDirection.DOWNSTREAM):
        pass

    def create_task(self, coroutine, name=None):
        return asyncio.create_task(coroutine, name=name)

    async def cancel_task(self, task, timeout=None):
        del timeout
        await cancel_and_join_task(task)


class _FakeStore:
    def __init__(self, create_error: Exception | None = None):
        self.create_error = create_error
        self.created_ids: list[str] = []

    def create(self, session_id: str) -> None:
        self.created_ids.append(session_id)
        if self.create_error is not None:
            raise self.create_error


def _raising_stream(error: Exception):
    """A respond_in_session_stream stand-in that raises on the first pull."""

    def factory(sid, msg, **kwargs):
        async def gen():
            raise error
            yield  # pragma: no cover — makes this an async generator

        return gen()

    return factory


def test_lookup_error_recovers_once_under_same_session_id_then_cancels():
    events = []
    proc = _FakeProcessor("sess-1", events.append)
    store = _FakeStore()

    async def run():
        with (
            patch("api.chat.respond_in_session_stream", _raising_stream(LookupError("sess-1"))),
            patch(
                "api.chat.chat_store_operation",
                lambda: nullcontext(SimpleNamespace(store=store)),
            ),
        ):
            await proc._run_agent_inference("test", DIRECTION)

    asyncio.run(run())
    assert {"type": "agent_cancelled"} in events
    assert store.created_ids == ["sess-1"]
    assert proc.session_id == "sess-1"


def test_tombstoned_session_stops_voice_recovery_without_resurrection():
    events = []
    proc = _FakeProcessor("sess-1", events.append)
    store = _FakeStore(SessionTombstonedError("sess-1"))
    stream_attempts = 0

    def missing_stream(sid, msg, **kwargs):
        nonlocal stream_attempts
        stream_attempts += 1
        return _raising_stream(LookupError(sid))(sid, msg, **kwargs)

    async def run():
        with (
            patch("api.chat.respond_in_session_stream", missing_stream),
            patch(
                "api.chat.chat_store_operation",
                lambda: nullcontext(SimpleNamespace(store=store)),
            ),
        ):
            await proc._run_agent_inference("test", DIRECTION)

    asyncio.run(run())
    assert {"type": "agent_cancelled"} in events
    assert stream_attempts == 1
    assert store.created_ids == ["sess-1"]
    assert proc.session_id == "sess-1"


def test_exception_sends_agent_reply_with_error_text():
    events = []
    proc = _FakeProcessor("sess-1", events.append)

    async def run():
        with patch("api.chat.respond_in_session_stream", _raising_stream(RuntimeError("boom"))):
            await proc._run_agent_inference("test", DIRECTION)

    asyncio.run(run())
    assert any(e.get("type") == "agent_reply" and e.get("role") == "assistant" for e in events)


def test_first_pull_failure_closes_stream_before_error_escapes():
    proc = _FakeProcessor("sess-1", None)
    closed = False

    class _FailingStream:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise RuntimeError("first pull failed")

        async def aclose(self):
            nonlocal closed
            closed = True

    async def run():
        with patch("api.chat.respond_in_session_stream", lambda *_args, **_kwargs: _FailingStream()):
            with pytest.raises(RuntimeError, match="first pull failed"):
                await proc._open_stream("test", {})

    asyncio.run(run())
    assert closed, "stream ownership never transferred, so _open_stream must close it"


def test_first_pull_and_rollback_failure_remain_owned_until_retry_succeeds():
    proc = _FakeProcessor("sess-retry", None)
    close_calls = 0

    class _RetryableCloseStream:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise RuntimeError("first pull failed")

        async def aclose(self):
            nonlocal close_calls
            close_calls += 1
            if close_calls == 1:
                raise RuntimeError("rollback close failed")

    async def run():
        stream = _RetryableCloseStream()
        with patch("api.chat.respond_in_session_stream", lambda *_args, **_kwargs: stream):
            with pytest.raises(BaseExceptionGroup) as raised:
                await proc._open_stream("test", {})

        assert [str(error) for error in raised.value.exceptions] == [
            "first pull failed",
            "rollback close failed",
        ]
        assert proc._response_streams.owned_count == 1
        assert proc._response_streams.failed_count == 1

        await proc._response_streams.retry_failed()
        assert proc._response_streams.owned_count == 0
        assert close_calls == 2

        await proc._response_streams.retry_failed()
        await proc._response_streams.aclose()
        assert close_calls == 2

    asyncio.run(run())


def test_process_frame_cannot_resume_construction_after_cleanup_closes_gate():
    async def run():
        proc = _FakeProcessor("sess-late", None)
        entered_base = asyncio.Event()
        release_base = asyncio.Event()
        stream_calls = 0

        async def blocking_base_process_frame(_self, _frame, _direction):
            entered_base.set()
            await release_base.wait()

        def fake_stream(*_args, **_kwargs):
            nonlocal stream_calls
            stream_calls += 1
            return _raising_stream(RuntimeError("must not open"))(*_args, **_kwargs)

        with (
            patch.object(FrameProcessor, "process_frame", blocking_base_process_frame),
            patch("api.chat.respond_in_session_stream", fake_stream),
        ):
            pending = asyncio.create_task(proc.process_frame(TranscriptionFrame("你好", "user", "t1"), DIRECTION))
            await entered_base.wait()
            proc._closing = True
            release_base.set()
            await pending

        assert proc._inference_task is None
        assert stream_calls == 0

    asyncio.run(run())


def test_streamed_chunks_are_pushed_incrementally_and_reply_event_is_full_text():
    """TextFrame chunks still stream into the pipeline (for TTS), but no
    agent_delta events go out anymore — subtitle sync now comes from
    pipeline.py's SubtitleSyncProcessor watching TTSTextFrame instead."""
    events = []
    pushed = []

    class _RecordingProcessor(_FakeProcessor):
        async def push_frame(self, frame, direction=FrameDirection.DOWNSTREAM):
            pushed.append(frame)

    proc = _RecordingProcessor("sess-1", events.append)

    def fake_stream(sid, msg, **kwargs):
        async def gen():
            yield "第一句。"
            yield "第二句。"

        return gen()

    async def run():
        with patch("api.chat.respond_in_session_stream", fake_stream):
            await proc._run_agent_inference("test", DIRECTION)

    asyncio.run(run())
    from pipecat.frames.frames import TextFrame

    texts = [f.text for f in pushed if isinstance(f, TextFrame)]
    assert texts == ["第一句。", "第二句。"]
    assert not any(e.get("type") == "agent_delta" for e in events)
    assert {"type": "agent_reply", "text": "第一句。第二句。", "role": "assistant"} in events


def test_injects_end_conversation_tool_whose_handler_emits_event():
    """The voice path must inject an end_conversation tool; its handler pushes
    {"type": "end_conversation"} to the client and returns a str for the LLM."""
    events = []
    proc = _FakeProcessor("sess-1", events.append)
    captured = {}

    def fake_stream(sid, msg, **kwargs):
        captured.update(kwargs)

        async def gen():
            yield "再會啦。"

        return gen()

    async def run():
        with patch("api.chat.respond_in_session_stream", fake_stream):
            await proc._run_agent_inference("再見", DIRECTION)

    asyncio.run(run())

    # The extra tool was injected with the exact contract name.
    extra_tools = captured["extra_tools"]
    assert [schema["function"]["name"] for schema, _ in extra_tools] == ["end_conversation"]
    assert "extra_system_prompt" in captured and captured["extra_system_prompt"]

    # Its handler emits the frozen JSON contract and returns a str.
    _, handler = extra_tools[0]
    result = asyncio.run(handler())
    assert {"type": "end_conversation"} in events
    assert isinstance(result, str) and result


def test_cancelling_response_that_already_started_sends_interruption_frame():
    """Regression test for the barge-in overlap bug: if the bot hasn't started
    audible playback yet (BargeInProcessor's _bot_speaking gate never fires),
    a new TranscriptionFrame arriving while the previous reply already pushed
    Start/TextFrame downstream must still send an InterruptionFrame — otherwise
    that reply's queued TTS audio plays on top of the new one."""
    pushed = []

    class _RecordingProcessor(_FakeProcessor):
        async def push_frame(self, frame, direction=FrameDirection.DOWNSTREAM):
            pushed.append(frame)

    proc = _RecordingProcessor("sess-1", None)
    hold = asyncio.Event()

    def fake_stream(sid, msg, **kwargs):
        async def gen():
            yield "第一句。"
            await hold.wait()

        return gen()

    async def run():
        with patch("api.chat.respond_in_session_stream", fake_stream):
            await proc.process_frame(TranscriptionFrame("你好", "user", "t1"), DIRECTION)
            # Drain the event loop until the background task has pushed
            # Start + the first TextFrame and is parked on hold.wait().
            for _ in range(5):
                await asyncio.sleep(0)
            assert proc._inference_state is not None
            assert proc._inference_state.started, "setup: first response must have started"
            assert not any(isinstance(f, InterruptionFrame) for f in pushed)

            # A second utterance arrives before the first reply finished.
            await proc.process_frame(TranscriptionFrame("閣再講一擺", "user", "t2"), DIRECTION)

            hold.set()
            for _ in range(5):
                await asyncio.sleep(0)
            if proc._inference_task and not proc._inference_task.done():
                proc._inference_task.cancel()
                await asyncio.sleep(0)

    asyncio.run(run())
    assert any(isinstance(f, InterruptionFrame) for f in pushed)


def test_cancel_awaits_old_task_so_stale_end_never_lands_after_new_start():
    """Regression test for the frame-order race: a fire-and-forget `.cancel()`
    on the old task would let its CancelledError handler (which pushes a stale
    LLMFullResponseEndFrame) interleave with and land after the new task's own
    LLMFullResponseStartFrame, breaking the TTS aggregator's Start/End pairing.
    `_cancel_inference_task()` awaits the old task first, so by the time
    process_frame() returns, old_task.done() must already be True and its End
    frame already flushed — a deterministic check, not one a bare `.cancel()`
    could satisfy by scheduling luck."""
    pushed = []

    class _RecordingProcessor(_FakeProcessor):
        async def push_frame(self, frame, direction=FrameDirection.DOWNSTREAM):
            pushed.append(frame)

    proc = _RecordingProcessor("sess-1", None)
    hold = asyncio.Event()

    def fake_stream(sid, msg, **kwargs):
        async def gen():
            yield "第一句。"
            await hold.wait()

        return gen()

    async def run():
        with patch("api.chat.respond_in_session_stream", fake_stream):
            # First utterance: task1 starts, pushes Start+Text, then parks on hold.wait().
            await proc.process_frame(TranscriptionFrame("你好", "user", "t1"), DIRECTION)
            for _ in range(5):
                await asyncio.sleep(0)
            assert proc._inference_state is not None
            assert proc._inference_state.started, "setup: first response must have started"
            task1 = proc._inference_task
            assert task1 is not None and not task1.done()

            # Second utterance arrives before task1 finished. process_frame must
            # not return until task1's cancellation cleanup has fully run.
            await proc.process_frame(TranscriptionFrame("閣再講一擺", "user", "t2"), DIRECTION)

            assert task1.done(), (
                "old inference task must be fully cancelled (its cleanup run to completion) before process_frame() returns control to the caller"
            )
            assert any(isinstance(f, LLMFullResponseEndFrame) for f in pushed), (
                "old task's stale End frame must already be flushed by the time "
                "process_frame() returns — otherwise it could still land after "
                "the new task's Start frame"
            )
            starts_so_far = [f for f in pushed if isinstance(f, LLMFullResponseStartFrame)]
            assert len(starts_so_far) == 1, "the new task must not have pushed its Start frame yet at this point"

            hold.set()
            for _ in range(5):
                await asyncio.sleep(0)

    asyncio.run(run())

    starts = [i for i, f in enumerate(pushed) if isinstance(f, LLMFullResponseStartFrame)]
    ends = [i for i, f in enumerate(pushed) if isinstance(f, LLMFullResponseEndFrame)]
    assert len(starts) == 2, f"expected two Start frames, got: {pushed}"
    assert len(ends) == 2, f"expected two End frames, got: {pushed}"
    assert ends[0] < starts[1], f"stale End landed after new Start: {pushed}"


def test_cancelled_inference_that_already_started_pushes_end_frame():
    """A cancelled task that already pushed LLMFullResponseStartFrame must also
    push a matching LLMFullResponseEndFrame — mirrors the exception-path
    handling below, which already does this; the CancelledError path didn't."""
    pushed = []

    class _RecordingProcessor(_FakeProcessor):
        async def push_frame(self, frame, direction=FrameDirection.DOWNSTREAM):
            pushed.append(frame)

    proc = _RecordingProcessor("sess-1", None)

    def fake_stream(sid, msg, **kwargs):
        async def gen():
            yield "第一句。"
            await asyncio.sleep(10)
            yield "unreachable"  # pragma: no cover

        return gen()

    async def run():
        with patch("api.chat.respond_in_session_stream", fake_stream):
            task = asyncio.create_task(proc._run_agent_inference("你好", DIRECTION))
            for _ in range(5):
                await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(run())
    assert any(isinstance(f, LLMFullResponseStartFrame) for f in pushed)
    assert any(isinstance(f, LLMFullResponseEndFrame) for f in pushed)


def test_cancel_waiter_cannot_interrupt_physical_inference_teardown():
    """Cancelling a cleanup waiter must not inject a second cancellation into
    the inference task while it is closing the response stream."""

    async def run():
        close_started = asyncio.Event()
        close_release = asyncio.Event()
        second_pull_started = asyncio.Event()
        pushed = []
        close_calls = 0
        close_cancelled = False

        class _BlockingCloseStream:
            def __init__(self) -> None:
                self.pull_count = 0

            def __aiter__(self):
                return self

            async def __anext__(self):
                self.pull_count += 1
                if self.pull_count == 1:
                    return "第一句。"
                second_pull_started.set()
                await asyncio.sleep(3600)
                raise StopAsyncIteration  # pragma: no cover

            async def aclose(self):
                nonlocal close_calls, close_cancelled
                close_calls += 1
                close_started.set()
                try:
                    await close_release.wait()
                except asyncio.CancelledError:
                    close_cancelled = True
                    raise

        class _RecordingProcessor(_FakeProcessor):
            async def push_frame(self, frame, direction=FrameDirection.DOWNSTREAM):
                pushed.append(frame)

        proc = _RecordingProcessor("sess-cancel-safe", None)
        stream = _BlockingCloseStream()
        with patch("api.chat.respond_in_session_stream", lambda *_args, **_kwargs: stream):
            physical_task = asyncio.create_task(proc._run_agent_inference("你好", DIRECTION))
            proc._inference_task = physical_task
            await asyncio.wait_for(second_pull_started.wait(), timeout=2.0)

            waiter = asyncio.create_task(proc._cancel_inference_task())
            await asyncio.wait_for(close_started.wait(), timeout=2.0)
            waiter.cancel()
            await asyncio.sleep(0)

            assert not waiter.done(), "cleanup waiter returned before physical stream close"
            assert not physical_task.done()
            assert physical_task.cancelling() == 1, "waiter cancellation was forwarded into inference"

            close_release.set()
            with pytest.raises(asyncio.CancelledError):
                await waiter

        assert physical_task.done() and physical_task.cancelled()
        assert close_calls == 1
        assert close_cancelled is False
        assert sum(isinstance(frame, LLMFullResponseStartFrame) for frame in pushed) == 1
        assert sum(isinstance(frame, LLMFullResponseEndFrame) for frame in pushed) == 1
        assert proc._inference_task is None

    asyncio.run(run())


# ---------------------------------------------------------------------------
# cleanup() must cancel the in-flight inference task
# ---------------------------------------------------------------------------


class _RealInitProcessor(TaigiBusAgentProcessor):
    """Real FrameProcessor.__init__ (so cleanup()'s super() chain is exercised),
    but create_task bypasses pipecat's task manager, which needs a live
    pipeline. setup() is never called, so FrameProcessor.cleanup() sees the
    base class's own input/process tasks as None, which it handles fine."""

    def create_task(self, coroutine, name=None):
        return asyncio.create_task(coroutine, name=name)

    async def cancel_task(self, task, timeout=None):
        del timeout
        await cancel_and_join_task(task)

    async def push_frame(self, frame, direction=FrameDirection.DOWNSTREAM):
        pass


def test_cleanup_cancels_in_flight_inference_task():
    """A client disconnecting mid-inference tears the pipeline down via
    cleanup(). pipecat's FrameProcessor.cleanup() only cancels the tasks it
    created itself, so without our override the inference task keeps running and
    holds the AgentSession + open LLM stream for the rest of the process life."""

    async def run():
        proc = _RealInitProcessor(session_id="sess-cleanup")
        running = asyncio.Event()
        reached_end = False

        async def _long_inference():
            nonlocal reached_end
            running.set()
            await asyncio.sleep(3600)
            reached_end = True  # pragma: no cover

        task = proc.create_task(_long_inference())
        proc._inference_task = task
        await asyncio.wait_for(running.wait(), timeout=2.0)
        assert not task.done()

        await proc.cleanup()

        assert task.done() and task.cancelled(), "inference task survived cleanup()"
        assert proc._inference_task is None
        assert reached_end is False

    asyncio.run(run())


def test_cleanup_is_safe_with_no_inference_in_flight():
    """cleanup() on an idle processor must not raise (and must still run the
    base-class cleanup chain)."""

    async def run():
        proc = _RealInitProcessor(session_id="sess-idle")
        await proc.cleanup()
        assert proc._inference_task is None

    asyncio.run(run())


def test_cleanup_retries_failed_stream_close_without_repeating_successful_base_cleanup():
    async def run():
        proc = _FakeProcessor("sess-cleanup-retry", None)
        close_calls = 0
        base_cleanup_calls = 0

        class _RetryableStream:
            async def aclose(self):
                nonlocal close_calls
                close_calls += 1
                if close_calls == 1:
                    raise RuntimeError("stream close failed")

        async def fake_base_cleanup(_self):
            nonlocal base_cleanup_calls
            base_cleanup_calls += 1

        stream = _RetryableStream()
        proc._response_streams.adopt(stream, lambda candidate: candidate.aclose())

        with patch.object(FrameProcessor, "cleanup", fake_base_cleanup):
            with pytest.raises(RuntimeError, match="stream close failed"):
                await proc.cleanup()

            assert proc._closing is True
            assert proc._response_streams.owned_count == 1
            assert proc._response_streams.failed_count == 1
            assert close_calls == 1
            assert base_cleanup_calls == 1

            await proc.cleanup()
            await proc.cleanup()

        assert proc._response_streams.owned_count == 0
        assert close_calls == 2
        assert base_cleanup_calls == 1

    asyncio.run(run())


def test_cleanup_waiter_cancellation_cannot_interrupt_physical_stream_close():
    async def run():
        proc = _FakeProcessor("sess-cleanup-cancel", None)
        close_started = asyncio.Event()
        close_release = asyncio.Event()
        close_calls = 0
        close_cancelled = False

        class _BlockingCloseStream:
            async def aclose(self):
                nonlocal close_calls, close_cancelled
                close_calls += 1
                close_started.set()
                try:
                    await close_release.wait()
                except asyncio.CancelledError:
                    close_cancelled = True
                    raise

        async def fake_base_cleanup(_self):
            return None

        stream = _BlockingCloseStream()
        proc._response_streams.adopt(stream, lambda candidate: candidate.aclose())

        with patch.object(FrameProcessor, "cleanup", fake_base_cleanup):
            waiter = asyncio.create_task(proc.cleanup())
            await close_started.wait()
            waiter.cancel()
            await asyncio.sleep(0)

            assert not waiter.done()
            assert proc._response_streams.owned_count == 1

            close_release.set()
            with pytest.raises(asyncio.CancelledError):
                await waiter

            await proc.cleanup()

        assert close_calls == 1
        assert close_cancelled is False
        assert proc._response_streams.owned_count == 0

    asyncio.run(run())


def test_inference_callback_registration_failure_cancels_and_joins_task():
    class _FailFirstCallbackTask(asyncio.Task):
        def __init__(self, coroutine):
            super().__init__(coroutine, loop=asyncio.get_running_loop())
            self._reject_next_callback = True

        def add_done_callback(self, callback, *, context=None):
            if self._reject_next_callback:
                self._reject_next_callback = False
                raise RuntimeError("callback registration failed")
            return super().add_done_callback(callback, context=context)

    class _CallbackFailingProcessor(_FakeProcessor):
        def __init__(self):
            super().__init__("sess-callback-failure", None)
            self.created_task = None

        def create_task(self, coroutine, name=None):
            del name
            task = _FailFirstCallbackTask(coroutine)
            self.created_task = task
            return task

    async def run():
        proc = _CallbackFailingProcessor()

        with pytest.raises(RuntimeError, match="callback registration failed"):
            await proc.process_frame(TranscriptionFrame("你好", "user", "t1"), DIRECTION)

        task = proc.created_task
        assert task is not None
        assert task.done()
        assert task.cancelled()
        assert proc._inference_task is None
        assert proc._inference_state is None

    asyncio.run(run())


def test_completed_inference_releases_task_and_state_references():
    async def run():
        proc = _FakeProcessor("sess-complete", None)

        def fake_stream(_sid, _msg, **_kwargs):
            async def gen():
                yield "完成。"

            return gen()

        with patch("api.chat.respond_in_session_stream", fake_stream):
            await proc.process_frame(TranscriptionFrame("你好", "user", "t1"), DIRECTION)
            task = proc._inference_task
            assert task is not None
            await task
            await asyncio.sleep(0)

        assert proc._inference_task is None
        assert proc._inference_state is None

    asyncio.run(run())


def test_failed_inference_blocks_next_transcript_until_failure_is_surfaced():
    async def run():
        proc = _FakeProcessor("sess-failed", None)
        failure = RuntimeError("response stream close failed")

        async def fail():
            raise failure

        task = proc.create_task(fail())
        state = _ResponseState()
        proc._inference_task = task
        proc._inference_state = state
        task.add_done_callback(proc._on_inference_done)
        while not task.done():
            await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert proc._inference_task is task
        assert proc._inference_state is state

        with pytest.raises(RuntimeError, match="response stream close failed") as raised:
            await proc.process_frame(TranscriptionFrame("新的問題", "user", "t2"), DIRECTION)

        assert raised.value is failure
        assert proc._inference_task is None
        assert proc._inference_state is None

    asyncio.run(run())


def test_cleanup_observes_failed_inference_and_still_closes_independent_owners():
    async def run():
        proc = _FakeProcessor("sess-failed-cleanup", None)
        failure = RuntimeError("inference teardown failed")
        base_cleanup_calls = 0

        async def fail():
            raise failure

        async def fake_base_cleanup(_self):
            nonlocal base_cleanup_calls
            base_cleanup_calls += 1

        task = proc.create_task(fail())
        proc._inference_task = task
        proc._inference_state = _ResponseState()
        task.add_done_callback(proc._on_inference_done)
        while not task.done():
            await asyncio.sleep(0)
        await asyncio.sleep(0)

        with patch.object(FrameProcessor, "cleanup", fake_base_cleanup):
            with pytest.raises(RuntimeError, match="inference teardown failed") as raised:
                await proc.cleanup()
            assert raised.value is failure
            assert proc._inference_task is None
            assert proc._response_streams.owned_count == 0
            assert base_cleanup_calls == 1

            await proc.cleanup()
            await proc.cleanup()

        assert proc._cleanup_complete
        assert proc._cleanup_task is None
        assert base_cleanup_calls == 1

    asyncio.run(run())


def test_stale_inference_callback_cannot_clear_replacement_task():
    async def run():
        proc = _FakeProcessor("sess-replaced", None)
        old_task = asyncio.create_task(asyncio.sleep(0))
        replacement_task = asyncio.create_task(asyncio.sleep(3600))
        replacement_state = _ResponseState()
        proc._inference_task = replacement_task
        proc._inference_state = replacement_state

        await old_task
        proc._on_inference_done(old_task)

        assert proc._inference_task is replacement_task
        assert proc._inference_state is replacement_state
        replacement_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await replacement_task

    asyncio.run(run())


def test_cleanup_flushes_response_end_frame_of_cancelled_inference():
    """The cancelled task's CancelledError handler still gets to push its
    closing LLMFullResponseEndFrame — cleanup() awaits the task rather than
    firing a bare cancel()."""
    pushed = []

    class _RecordingProcessor(_RealInitProcessor):
        async def push_frame(self, frame, direction=FrameDirection.DOWNSTREAM):
            pushed.append(frame)

    def fake_stream(sid, msg, **kwargs):
        async def gen():
            yield "第一句。"
            await asyncio.sleep(10)
            yield "unreachable"  # pragma: no cover

        return gen()

    async def run():
        proc = _RecordingProcessor(session_id="sess-flush")
        with patch("api.chat.respond_in_session_stream", fake_stream):
            proc._inference_task = proc.create_task(proc._run_agent_inference("你好", DIRECTION))
            for _ in range(5):
                await asyncio.sleep(0)
            await proc.cleanup()

    asyncio.run(run())
    assert any(isinstance(f, LLMFullResponseStartFrame) for f in pushed)
    assert any(isinstance(f, LLMFullResponseEndFrame) for f in pushed)
