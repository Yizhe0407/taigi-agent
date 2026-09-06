"""Deterministic tests for shared async lifecycle primitives."""

import asyncio
import inspect
import threading
from typing import cast

import pytest

from async_lifecycle import AsyncResourceOwner, ReclaimingAsyncLock, create_lifecycle_task, run_in_thread


def test_lifecycle_task_publishes_before_inner_coroutine_under_eager_factory():
    async def run() -> None:
        loop = asyncio.get_running_loop()
        original_factory = loop.get_task_factory()
        published: asyncio.Task[None] | None = None
        observed: list[bool] = []

        async def inner() -> None:
            observed.append(published is asyncio.current_task())

        loop.set_task_factory(asyncio.eager_task_factory)
        try:
            task = create_lifecycle_task(inner(), name="publication-test")
            published = task
            await task
        finally:
            loop.set_task_factory(original_factory)

        assert observed == [True]

    asyncio.run(run())


def test_lifecycle_task_closes_inner_coroutine_when_task_creation_fails():
    class _RejectingLoop:
        def create_task(self, _coroutine, *, name=None):
            del name
            raise RuntimeError("task factory rejected task")

    async def inner() -> None:
        raise AssertionError("unstarted coroutine ran")

    coroutine = inner()
    with pytest.raises(RuntimeError, match="task factory rejected task"):
        create_lifecycle_task(
            coroutine,
            name="rejected-task",
            loop=cast(asyncio.AbstractEventLoop, _RejectingLoop()),
        )

    assert inspect.getcoroutinestate(coroutine) is inspect.CORO_CLOSED


def test_run_in_thread_defers_waiter_cancellation_until_worker_finishes():
    release_worker = threading.Event()
    worker_finished = threading.Event()

    async def run() -> None:
        loop = asyncio.get_running_loop()
        worker_started = asyncio.Event()

        def blocking_work() -> str:
            loop.call_soon_threadsafe(worker_started.set)
            release_worker.wait()
            worker_finished.set()
            return "done"

        waiter = asyncio.create_task(run_in_thread(blocking_work))
        await worker_started.wait()

        waiter.cancel()
        await asyncio.sleep(0)  # cancellation reached the shielded physical-task join
        assert not waiter.done()
        assert not worker_finished.is_set()

        release_worker.set()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert worker_finished.is_set()

    asyncio.run(run())


def test_reclaiming_lock_serializes_one_contention_set_and_retires_itself():
    lock = ReclaimingAsyncLock("test lock")

    async def run() -> None:
        first_entered = asyncio.Event()
        release_first = asyncio.Event()
        second_entered = asyncio.Event()
        order: list[str] = []

        async def first() -> None:
            async with lock.acquire():
                order.append("first")
                first_entered.set()
                await release_first.wait()

        async def second() -> None:
            async with lock.acquire():
                order.append("second")
                second_entered.set()

        first_task = asyncio.create_task(first())
        await first_entered.wait()
        second_task = asyncio.create_task(second())
        await asyncio.sleep(0)

        assert lock.active_users == 2
        assert not second_entered.is_set()

        release_first.set()
        await first_task
        await second_task

        assert second_entered.is_set()
        assert order == ["first", "second"]
        assert lock.active_users == 0

    asyncio.run(run())


def test_reclaiming_lock_retires_a_cancelled_waiter():
    lock = ReclaimingAsyncLock("test lock")

    async def run() -> None:
        holder_entered = asyncio.Event()
        release_holder = asyncio.Event()

        async def holder() -> None:
            async with lock.acquire():
                holder_entered.set()
                await release_holder.wait()

        async def waiter() -> None:
            async with lock.acquire():
                raise AssertionError("cancelled waiter acquired the lock")

        holder_task = asyncio.create_task(holder())
        await holder_entered.wait()
        waiter_task = asyncio.create_task(waiter())
        await asyncio.sleep(0)
        assert lock.active_users == 2

        waiter_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter_task
        assert lock.active_users == 1

        release_holder.set()
        await holder_task
        assert lock.active_users == 0

    asyncio.run(run())


def test_reclaiming_lock_can_serve_sequential_event_loop_generations():
    lock = ReclaimingAsyncLock("test lock")

    async def use_once() -> None:
        async with lock.acquire():
            assert lock.active_users == 1

    asyncio.run(use_once())
    assert lock.active_users == 0
    asyncio.run(use_once())
    assert lock.active_users == 0


def test_reclaiming_lock_rejects_a_rival_loop_while_generation_is_active():
    lock = ReclaimingAsyncLock("test lock")
    holder_entered = threading.Event()
    release_holder = threading.Event()
    holder_errors: list[BaseException] = []

    async def hold_from_owner_loop() -> None:
        async with lock.acquire():
            holder_entered.set()
            release_holder.wait()

    def owner_thread() -> None:
        try:
            asyncio.run(hold_from_owner_loop())
        except BaseException as error:  # surface thread failures in the test thread
            holder_errors.append(error)

    thread = threading.Thread(target=owner_thread, name="lock-owner-loop")
    thread.start()
    holder_entered.wait()

    async def contend_from_rival_loop() -> None:
        async with lock.acquire():
            raise AssertionError("rival loop acquired the active generation")

    try:
        with pytest.raises(RuntimeError, match="different event loop"):
            asyncio.run(contend_from_rival_loop())
    finally:
        release_holder.set()
        thread.join()

    assert holder_errors == []
    assert lock.active_users == 0


def test_async_resource_owner_close_admission_is_synchronous_and_joins_existing_lease():
    async def run() -> None:
        owner: AsyncResourceOwner[object] = AsyncResourceOwner("test resources")
        acquisition = owner.begin_acquisition()

        owner.close_admission()

        assert owner.closing
        assert not owner.closed
        assert owner.pending_count == 1
        with pytest.raises(RuntimeError, match="owner is closed"):
            owner.begin_acquisition()

        shutdown = asyncio.create_task(owner.aclose())
        await asyncio.sleep(0)
        assert not shutdown.done()

        owner.abort_acquisition(acquisition)
        await shutdown

        assert owner.closed
        assert owner.pending_count == 0

    asyncio.run(run())


def test_async_resource_owner_waits_for_pending_constructor_and_closes_late_resource():
    async def run() -> None:
        owner: AsyncResourceOwner[object] = AsyncResourceOwner("test resources")
        acquisition = owner.begin_acquisition()
        resource = object()
        closed: list[object] = []

        async def close(value: object) -> None:
            closed.append(value)

        shutdown = asyncio.create_task(owner.aclose())
        await asyncio.sleep(0)
        assert owner.closing
        assert not shutdown.done()

        entry = owner.finish_acquisition(acquisition, resource, close)
        await shutdown

        assert closed == [resource]
        assert not owner.owns(entry)
        assert owner.closed
        with pytest.raises(RuntimeError, match="owner is closed"):
            owner.begin_acquisition()

    asyncio.run(run())


def test_async_resource_owner_shutdown_task_creation_failure_keeps_gate_closed_and_debt_retryable():
    async def run() -> None:
        owner: AsyncResourceOwner[object] = AsyncResourceOwner("test resources")
        closed: list[object] = []
        resource = object()

        async def close(value: object) -> None:
            closed.append(value)

        entry = owner.adopt(resource, close)
        loop = asyncio.get_running_loop()
        original_factory = loop.get_task_factory()

        def reject_task(_loop, _coroutine, **_kwargs):
            raise RuntimeError("task factory rejected task")

        loop.set_task_factory(reject_task)
        try:
            with pytest.raises(RuntimeError, match="task factory rejected task"):
                await owner.aclose()
        finally:
            loop.set_task_factory(original_factory)

        assert owner.closing
        assert not owner.closed
        assert owner.owns(entry)
        assert closed == []
        with pytest.raises(RuntimeError, match="owner is closed"):
            owner.begin_acquisition()

        await owner.aclose()

        assert closed == [resource]
        assert owner.closed
        assert not owner.owns(entry)

    asyncio.run(run())


def test_async_resource_owner_defers_waiter_cancellation_until_physical_close_finishes():
    async def run() -> None:
        owner: AsyncResourceOwner[object] = AsyncResourceOwner("test resources")
        close_started = asyncio.Event()
        release_close = asyncio.Event()

        async def close(_resource: object) -> None:
            close_started.set()
            await release_close.wait()

        owner.adopt(object(), close)
        waiter = asyncio.create_task(owner.aclose())
        await close_started.wait()

        waiter.cancel()
        await asyncio.sleep(0)
        assert not waiter.done()
        assert not owner.closed

        release_close.set()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert owner.closed
        assert owner.owned_count == 0

    asyncio.run(run())


def test_async_resource_owner_retains_failed_resource_and_retries_same_entry():
    async def run() -> None:
        owner: AsyncResourceOwner[object] = AsyncResourceOwner("test resources")
        resource = object()
        failure = RuntimeError("physical close failed")
        attempts: list[object] = []

        async def close(value: object) -> None:
            attempts.append(value)
            if len(attempts) == 1:
                raise failure

        entry = owner.adopt(resource, close)
        with pytest.raises(RuntimeError, match="physical close failed") as raised:
            await owner.release(entry)

        assert raised.value is failure
        assert owner.owns(entry)
        assert owner.failed_count == 1
        assert entry.close_task is None

        await owner.release(entry)
        assert attempts == [resource, resource]
        assert not owner.owns(entry)
        assert owner.owned_count == 0

    asyncio.run(run())


def test_async_resource_owner_concurrent_release_and_shutdown_join_one_close_task():
    async def run() -> None:
        owner: AsyncResourceOwner[object] = AsyncResourceOwner("test resources")
        close_started = asyncio.Event()
        release_close = asyncio.Event()
        close_calls = 0

        async def close(_resource: object) -> None:
            nonlocal close_calls
            close_calls += 1
            close_started.set()
            await release_close.wait()

        entry = owner.adopt(object(), close)
        release_waiter = asyncio.create_task(owner.release(entry))
        await close_started.wait()
        shutdown_waiter = asyncio.create_task(owner.aclose())
        await asyncio.sleep(0)

        assert close_calls == 1
        assert entry.close_task is not None

        release_close.set()
        await release_waiter
        await shutdown_waiter

        assert close_calls == 1
        assert owner.closed
        assert owner.owned_count == 0

    asyncio.run(run())


def test_async_resource_owner_task_creation_failure_restores_retryable_entry_state():
    async def run() -> None:
        owner: AsyncResourceOwner[object] = AsyncResourceOwner("test resources")
        close_calls = 0

        async def close(_resource: object) -> None:
            nonlocal close_calls
            close_calls += 1

        entry = owner.adopt(object(), close)
        loop = asyncio.get_running_loop()
        original_factory = loop.get_task_factory()

        def reject_task(_loop, _coroutine, **_kwargs):
            raise RuntimeError("task factory rejected task")

        loop.set_task_factory(reject_task)
        try:
            with pytest.raises(RuntimeError, match="task factory rejected task"):
                await owner.release(entry)
        finally:
            loop.set_task_factory(original_factory)

        assert close_calls == 0
        assert entry.state == "active"
        assert entry.close_task is None
        assert owner.owns(entry)

        await owner.release(entry)
        assert close_calls == 1
        assert not owner.owns(entry)

    asyncio.run(run())


def test_async_resource_owner_retires_mutate_then_throw_physical_close():
    async def run() -> None:
        owner: AsyncResourceOwner[object] = AsyncResourceOwner("test resources")
        failure = RuntimeError("callback failed after physical close")
        entry = None

        async def close(_resource: object) -> None:
            assert entry is not None
            owner.retire_closed(entry)
            raise failure

        entry = owner.adopt(object(), close)
        with pytest.raises(RuntimeError, match="callback failed after physical close") as raised:
            await owner.release(entry)

        assert raised.value is failure
        assert not owner.owns(entry)
        assert owner.failed_count == 0
        await owner.release(entry)

    asyncio.run(run())
