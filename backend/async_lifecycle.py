"""Shared primitives for cancellation-safe asynchronous resource teardown."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Literal


async def _run_after_task_publication[T](
    start_gate: asyncio.Event,
    coroutine: Coroutine[Any, Any, T],
) -> T:
    """Keep an inner coroutine inert until its task can be published by its owner."""
    started = False
    try:
        await start_gate.wait()
        started = True
        return await coroutine
    finally:
        if not started:
            coroutine.close()


def create_lifecycle_task[T](
    coroutine: Coroutine[Any, Any, T],
    *,
    name: str,
    loop: asyncio.AbstractEventLoop | None = None,
) -> asyncio.Task[T]:
    """Create a task without allowing eager execution before owner publication.

    Python 3.12's eager task factory may enter a coroutine synchronously inside
    ``create_task``.  Lifecycle code normally publishes the returned task into
    an authoritative slot or set immediately afterwards; running the real
    coroutine before that publication creates a re-entrancy window where close
    or replacement logic cannot see the resource it already started.

    The physical task therefore starts in a private gate waiter.  Opening the
    gate schedules, rather than synchronously executes, its continuation, so
    callers can publish the returned task and register callbacks before the
    inner coroutine can run.  If task construction fails, both wrapper and
    never-started inner coroutine are closed transactionally.
    """

    start_gate = asyncio.Event()
    wrapper = _run_after_task_publication(start_gate, coroutine)
    try:
        if loop is None:
            task = asyncio.create_task(wrapper, name=name)
        else:
            task = loop.create_task(wrapper, name=name)
    except BaseException:
        wrapper.close()
        coroutine.close()
        raise
    start_gate.set()
    return task


async def _wait_for_task[T](task: asyncio.Task[T]) -> asyncio.CancelledError | None:
    """Wait for ``task`` while retaining cancellation of the current waiter."""
    waiter_cancelled: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as error:
            waiter = asyncio.current_task()
            # ``shield`` raises CancelledError for either side of the boundary:
            # the waiter may have been cancelled, or the physical task may have
            # finished by cancellation.  A cancellation request recorded on the
            # current task identifies the former even if both happen in the same
            # event-loop turn.
            if not task.cancelled() or (waiter is not None and waiter.cancelling()):
                if waiter_cancelled is None:
                    waiter_cancelled = error
    return waiter_cancelled


async def join_task[T](task: asyncio.Task[T]) -> T:
    """Join one physical lifecycle task without transferring cancellation to it.

    A caller may be cancelled while it is waiting for teardown, but cancellation
    of that waiter must never become cancellation of the resource owner.  Keep
    shielding until the physical task settles, surface its failure first, then
    restore the waiter's original cancellation.
    """
    waiter_cancelled = await _wait_for_task(task)
    result = task.result()
    if waiter_cancelled is not None:
        raise waiter_cancelled
    return result


async def run_in_thread[T, **P](
    func: Callable[P, T],
    /,
    *args: P.args,
    **kwargs: P.kwargs,
) -> T:
    """Run blocking work without letting waiter cancellation detach its thread.

    ``asyncio.to_thread`` only cancels the coroutine that waits for the worker;
    it cannot stop a thread that has already begun.  Scheduling that coroutine
    in one strongly-held task and joining it through :func:`join_task` preserves
    the real lifetime boundary: a cancelled caller resumes cancellation only
    after the physical work has finished and its result or failure was observed.

    Task creation is transactional.  If the loop rejects the task, close the
    never-started ``to_thread`` coroutine instead of leaving it for GC to warn
    about or accidentally schedule later.
    """

    coroutine = asyncio.to_thread(func, *args, **kwargs)
    label = getattr(func, "__qualname__", getattr(func, "__name__", "blocking-work"))
    task = create_lifecycle_task(coroutine, name=f"thread:{label}")
    return await join_task(task)


async def cancel_and_join_task[T](task: asyncio.Task[T]) -> None:
    """Cancel one physical task exactly once and wait for its teardown.

    Multiple lifecycle owners may converge on the same task.  Only the first
    caller requests cancellation; every caller then joins the same physical
    teardown without forwarding its own cancellation into that task.  The
    task's expected ``CancelledError`` is consumed, cleanup failures win over a
    waiter's cancellation, and a cancelled waiter resumes cancellation only
    after physical teardown has finished.
    """
    if not task.done() and task.cancelling() == 0:
        task.cancel()

    waiter_cancelled = await _wait_for_task(task)
    try:
        task.result()
    except asyncio.CancelledError:
        # Cancellation is the requested terminal state of the physical task.
        pass

    if waiter_cancelled is not None:
        raise waiter_cancelled


@dataclass(eq=False)
class _ReclaimingLockState:
    loop: asyncio.AbstractEventLoop
    lock: asyncio.Lock
    users: int = 0


class ReclaimingAsyncLock:
    """A mutual-exclusion lock retained only while holders or waiters exist.

    A long-lived module/provider object must not permanently retain an
    ``asyncio.Lock`` that became bound to an earlier event-loop generation.
    This owner creates one authoritative lock for the current contention set,
    reference-counts every holder and waiter before their first ``await``, and
    retires the lock synchronously after the final user leaves.  A different
    event loop is rejected while the current generation is still active rather
    than silently minting a rival lock and breaking mutual exclusion.
    """

    def __init__(self, label: str) -> None:
        self._label = label
        self._state: _ReclaimingLockState | None = None
        self._state_guard = threading.Lock()

    @property
    def active_users(self) -> int:
        with self._state_guard:
            state = self._state
            return state.users if state is not None else 0

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[None]:
        loop = asyncio.get_running_loop()
        with self._state_guard:
            state = self._state
            if state is None:
                state = _ReclaimingLockState(loop=loop, lock=asyncio.Lock())
                self._state = state
            elif state.loop is not loop:
                raise RuntimeError(f"{self._label} is still owned by a different event loop")
            state.users += 1

        try:
            async with state.lock:
                yield
        finally:
            with self._state_guard:
                state.users -= 1
                if state.users == 0 and self._state is state:
                    self._state = None


@dataclass(eq=False)
class OwnedAsyncResource[T]:
    """One resource strongly retained until its asynchronous close succeeds."""

    resource: T
    close: Callable[[T], Awaitable[None]]
    state: Literal["active", "closing", "failed"] = "active"
    close_task: asyncio.Task[None] | None = None
    physically_closed: bool = False


class _AsyncResourceAcquisition:
    """Tracks an acquisition that crossed an ``await`` before adoption."""

    __slots__ = ("owner",)

    def __init__(self, owner: AsyncResourceOwner) -> None:
        self.owner = owner


class AsyncResourceOwner[T]:
    """Authoritative, retryable owner for asynchronously closed resources.

    Resources stay strongly referenced until their close operation succeeds.
    A failed close is therefore not converted into a log-only leak: the same
    entry remains available to ``retry_failed()`` or the terminal ``aclose()``
    lifecycle.  Every physical close runs in one owned task, so cancellation of
    a waiter cannot cancel resource release and concurrent callers join rather
    than duplicate it.

    ``begin_acquisition()``/``finish_acquisition()`` cover constructors that
    cross an ``await``.  Terminal close permanently shuts the acquisition gate,
    waits for every constructor already in flight to either adopt or abort, and
    only then drains the resulting resource set.  This prevents a late
    continuation from resurrecting a resource after shutdown took its snapshot.
    """

    def __init__(self, label: str) -> None:
        self._label = label
        self._entries: set[OwnedAsyncResource[T]] = set()
        self._pending: set[_AsyncResourceAcquisition] = set()
        self._pending_empty = asyncio.Event()
        self._pending_empty.set()
        self._closing = False
        self._closed = False
        self._shutdown_task: asyncio.Task[None] | None = None

    @property
    def closing(self) -> bool:
        return self._closing

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def owned_count(self) -> int:
        return len(self._entries)

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    @property
    def failed_count(self) -> int:
        return sum(entry.state == "failed" for entry in self._entries)

    def owns(self, entry: OwnedAsyncResource[T]) -> bool:
        """Return whether release still needs to prove this resource closed."""
        return entry in self._entries

    def retire_closed(self, entry: OwnedAsyncResource[T]) -> None:
        """Retire a resource that reported its own physical close.

        Some resources can close independently and notify their owner from
        inside the same physical close operation used by :meth:`release`.
        Joining that operation from its own callback would deadlock.  Record
        the physical-close fact synchronously instead: an externally closed
        entry is removed immediately, while an entry whose close task is still
        running is removed by that task after it returns (or even if it reports
        an error after the resource has demonstrably closed).
        """
        if entry not in self._entries:
            return
        entry.physically_closed = True
        if entry.close_task is None:
            self._entries.discard(entry)

    def begin_acquisition(self) -> _AsyncResourceAcquisition:
        """Reserve ownership before an async constructor starts."""
        if self._closing:
            raise RuntimeError(f"{self._label} owner is closed")
        acquisition = _AsyncResourceAcquisition(self)
        self._pending.add(acquisition)
        self._pending_empty.clear()
        return acquisition

    def close_admission(self) -> None:
        """Permanently reject new acquisitions without waiting for teardown.

        Lifecycle coordinators sometimes have to publish a close task only
        after synchronously removing a resource from a live registry.  Closing
        this existing gate first eliminates the publication window in which a
        retained borrower could begin work against the retired generation.
        ``aclose()`` remains the sole operation that joins pending acquisitions
        and releases resources; this method creates no parallel lifecycle state.
        """
        self._closing = True

    def finish_acquisition(
        self,
        acquisition: _AsyncResourceAcquisition,
        resource: T,
        close: Callable[[T], Awaitable[None]],
    ) -> OwnedAsyncResource[T]:
        """Adopt a successfully constructed resource before releasing its lease."""
        self._validate_acquisition(acquisition)
        entry = OwnedAsyncResource(resource=resource, close=close)
        self._entries.add(entry)
        self._finish_acquisition(acquisition)
        return entry

    def abort_acquisition(self, acquisition: _AsyncResourceAcquisition) -> None:
        """Release a constructor lease that produced no resource."""
        self._validate_acquisition(acquisition)
        self._finish_acquisition(acquisition)

    def adopt(
        self,
        resource: T,
        close: Callable[[T], Awaitable[None]],
    ) -> OwnedAsyncResource[T]:
        """Adopt a synchronously obtained resource behind the permanent gate."""
        acquisition = self.begin_acquisition()
        return self.finish_acquisition(acquisition, resource, close)

    def _validate_acquisition(self, acquisition: _AsyncResourceAcquisition) -> None:
        if acquisition.owner is not self or acquisition not in self._pending:
            raise RuntimeError(f"{self._label} acquisition is not owned or was already completed")

    def _finish_acquisition(self, acquisition: _AsyncResourceAcquisition) -> None:
        self._pending.remove(acquisition)
        if not self._pending:
            self._pending_empty.set()

    async def _close_entry(self, entry: OwnedAsyncResource[T]) -> None:
        try:
            await entry.close(entry.resource)
        except BaseException:
            if entry.physically_closed:
                self._entries.discard(entry)
            else:
                entry.state = "failed"
            raise
        else:
            entry.physically_closed = True
            self._entries.discard(entry)

    async def release(self, entry: OwnedAsyncResource[T]) -> None:
        """Close one resource once; retain it after failure for a later retry."""
        if entry not in self._entries:
            return

        task = entry.close_task
        if task is None:
            previous_state = entry.state
            entry.state = "closing"
            try:
                task = create_lifecycle_task(
                    self._close_entry(entry),
                    name=f"{self._label}-close",
                )
            except BaseException:
                entry.state = previous_state
                raise
            entry.close_task = task

        try:
            await join_task(task)
        finally:
            # A successful close removed the entry.  A failed/cancelled physical
            # close leaves it in ``failed`` and drops only the task identity so a
            # later lifecycle owner can create exactly one retry task.
            if entry.close_task is task and task.done():
                entry.close_task = None

    async def _release_entries(self, entries: tuple[OwnedAsyncResource[T], ...]) -> None:
        errors: list[BaseException] = []
        for entry in entries:
            if entry not in self._entries:
                continue
            try:
                await self.release(entry)
            except BaseException as error:  # noqa: BLE001 — continue independent releases
                errors.append(error)

        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise BaseExceptionGroup(f"Failed to close {self._label}", errors)

    async def retry_failed(self) -> None:
        """Retry each previously failed resource once without closing the gate."""
        if self._closing:
            raise RuntimeError(f"{self._label} owner is closing")
        failed = tuple(entry for entry in self._entries if entry.state == "failed")
        if not failed:
            return
        task = create_lifecycle_task(
            self._release_entries(failed),
            name=f"{self._label}-retry",
        )
        await join_task(task)

    async def _finalize(self) -> None:
        await self._pending_empty.wait()
        await self._release_entries(tuple(self._entries))

    async def aclose(self) -> None:
        """Permanently close acquisition, drain all resources, and allow retry."""
        if self._closed:
            return
        self.close_admission()
        task = self._shutdown_task
        if task is None:
            task = create_lifecycle_task(
                self._finalize(),
                name=f"{self._label}-shutdown",
            )
            self._shutdown_task = task

        try:
            await join_task(task)
        finally:
            if self._shutdown_task is task and task.done():
                if not task.cancelled() and task.exception() is None:
                    self._closed = True
                # A failed finalizer keeps the gate closed and resources owned,
                # but releases the task identity so a later ``aclose`` can retry.
                self._shutdown_task = None
