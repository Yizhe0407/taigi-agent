"""Converter construction and worker-pool lifecycle tests."""

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest

import pipeline.text_processor as text_processor


def test_concurrent_cold_start_constructs_hanlo_converter_exactly_once():
    """Regression test for finding 3: process() runs on a 4-worker thread pool,
    so several cold-start calls can race into _get_hanlo() at once. Without a
    lock, two threads could both observe `_hanlo_converter is None` and each
    construct (and one discard) a duplicate — wasted disk/CPU work loading the
    lexicon. The double-checked lock must collapse concurrent cold starts into
    a single construction."""
    build_count = 0
    build_lock = threading.Lock()

    class _SlowConverter:
        def __init__(self):
            nonlocal build_count
            with build_lock:
                build_count += 1
            # Widen the race window so concurrent callers actually overlap
            # inside the unlocked region instead of serializing by luck.
            time.sleep(0.05)

    with (
        patch.object(text_processor, "_hanlo_converter", None),
        patch("pipeline.text_processor.TaigiConverter", _SlowConverter),
    ):
        threads = [threading.Thread(target=text_processor._get_hanlo) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    assert build_count == 1


def test_concurrent_cold_start_constructs_taibun_converter_exactly_once():
    build_count = 0
    build_lock = threading.Lock()

    class _SlowConverter:
        def __init__(self, **kwargs):
            nonlocal build_count
            with build_lock:
                build_count += 1
            time.sleep(0.05)

    with (
        patch.object(text_processor, "_taibun_converter", None),
        patch("pipeline.text_processor.TaibunConverter", _SlowConverter),
    ):
        threads = [threading.Thread(target=text_processor._get_taibun) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    assert build_count == 1


def test_cancelled_waiter_joins_physical_conversion_before_resuming_cancellation():
    owner = text_processor._TextProcessorExecutorOwner()
    release_worker = threading.Event()
    worker_finished = threading.Event()

    async def run() -> None:
        loop = asyncio.get_running_loop()
        worker_started = asyncio.Event()

        def blocking_conversion() -> str:
            loop.call_soon_threadsafe(worker_started.set)
            release_worker.wait()
            worker_finished.set()
            return "done"

        waiter = asyncio.create_task(owner.run(blocking_conversion))
        await worker_started.wait()

        waiter.cancel()
        await asyncio.sleep(0)  # deterministic loop marker: cancellation reached join_task
        assert not waiter.done()
        assert owner.owned_count == 1

        release_worker.set()
        with pytest.raises(asyncio.CancelledError):
            await waiter

        assert worker_finished.is_set()
        assert owner.owned_count == 0
        await owner.aclose()

    asyncio.run(run())


def test_shutdown_closes_gate_and_waits_for_in_flight_conversion():
    owner = text_processor._TextProcessorExecutorOwner()
    release_worker = threading.Event()

    async def run() -> None:
        loop = asyncio.get_running_loop()
        worker_started = asyncio.Event()

        def blocking_conversion() -> str:
            loop.call_soon_threadsafe(worker_started.set)
            release_worker.wait()
            return "done"

        conversion = asyncio.create_task(owner.run(blocking_conversion))
        await worker_started.wait()

        shutdown = asyncio.create_task(owner.aclose())
        await asyncio.sleep(0)  # let aclose close the permanent acquisition gate
        assert owner.closing
        assert not shutdown.done()
        with pytest.raises(RuntimeError, match="shutting down"):
            await owner.run(lambda: "late")

        release_worker.set()
        assert await conversion == "done"
        await shutdown

        assert owner.closed
        assert owner.owned_count == 0
        assert not owner.has_executor

    asyncio.run(run())


def test_retry_does_not_repeat_executor_shutdown_after_physical_close():
    class RecordingExecutor(ThreadPoolExecutor):
        shutdown_calls = 0

        def shutdown(self, wait=True, *, cancel_futures=False):
            self.shutdown_calls += 1
            return super().shutdown(wait=wait, cancel_futures=cancel_futures)

    executor = RecordingExecutor(max_workers=1)
    owner = text_processor._TextProcessorExecutorOwner(lambda: executor)

    async def run() -> None:
        assert await owner.run(lambda: "ready") == "ready"

        async def pending_job() -> None:
            await asyncio.Event().wait()

        cancelled_job = asyncio.create_task(pending_job())
        cancelled_job.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled_job
        owner._jobs.add(cancelled_job)

        with pytest.raises(asyncio.CancelledError):
            await owner.aclose()

        assert owner.closing
        assert not owner.closed
        assert owner.has_executor
        assert executor.shutdown_calls == 1

        await owner.aclose()

        assert owner.closed
        assert not owner.has_executor
        assert executor.shutdown_calls == 1

    asyncio.run(run())


def test_executor_shutdown_runs_off_loop_and_remains_physically_joined():
    async def run() -> None:
        loop = asyncio.get_running_loop()
        event_loop_thread = threading.get_ident()
        shutdown_started = asyncio.Event()
        allow_shutdown = threading.Event()

        class BlockingShutdownExecutor(ThreadPoolExecutor):
            shutdown_thread: int | None = None

            def shutdown(self, wait=True, *, cancel_futures=False):
                self.shutdown_thread = threading.get_ident()
                loop.call_soon_threadsafe(shutdown_started.set)
                allow_shutdown.wait()
                return super().shutdown(wait=wait, cancel_futures=cancel_futures)

        executor = BlockingShutdownExecutor(max_workers=1)
        owner = text_processor._TextProcessorExecutorOwner(lambda: executor)
        assert await owner.run(lambda: "ready") == "ready"

        shutdown = asyncio.create_task(owner.aclose())
        await shutdown_started.wait()

        marker_ran = False

        async def mark_loop_progress() -> None:
            nonlocal marker_ran
            marker_ran = True

        await mark_loop_progress()
        assert marker_ran
        assert executor.shutdown_thread is not None
        assert executor.shutdown_thread != event_loop_thread
        assert not shutdown.done()

        allow_shutdown.set()
        await shutdown

        assert owner.closed
        assert not owner.has_executor

    asyncio.run(run())
