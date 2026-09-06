"""Text conversion pipeline: Mandarin → 漢羅 (HanloFlow) → Tailo (Taibun).

Both converters are expensive to initialise (loads data artifacts from disk),
so they're held as module-level lazy singletons — one instance per process.
The worker pool is different: it belongs to one application generation and is
opened/closed explicitly by the API lifespan.

Usage::

    from pipeline.text_processor import process

    result = process("307 公車幾點到？")
    result.hanlo   # 漢羅混合文字
    result.tailo   # Tailo 台羅（送 TTS）
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import partial
from typing import Any, cast

from taibun import Converter as TaibunConverter  # type: ignore[import-untyped]
from taigi_converter import TaigiConverter  # type: ignore[import-untyped]

from async_lifecycle import create_lifecycle_task, join_task, run_in_thread

_hanlo_converter: TaigiConverter | None = None
_taibun_converter: TaibunConverter | None = None
# Guards the two lazy-init blocks below. process() runs on the ThreadPoolExecutor
# below (up to 4 workers), so two cold-start calls landing on different worker
# threads at once could otherwise both see `None` and construct + discard a
# duplicate converter (wasted disk/CPU work, not a correctness bug — reads
# after init are lock-free since neither converter mutates its own state, see
# _MAX_WORKERS comment below).
_init_lock = threading.Lock()

# 4 workers：兩個 converter 的 convert()/get() 只讀初始化時建好的 lexicon/trie/dict，
# 不寫 self 狀態（已逐一檢視 taigi_converter.converter / taibun.taibun 原始碼確認），
# 併發呼叫是安全的。單 worker 會讓多 session 的文字轉換互相排隊、拉高首音延遲；
# 拉高到多 worker 讓不同 session 平行跑，仍留在 thread pool 內不卡 event loop
# （首次呼叫的 model 載入也在這裡完成）。數字與 services/taigi_tts.py 的
# TTS_MAX_CONCURRENCY 對齊，非量測得出的最佳值。
_MAX_WORKERS = 4


@dataclass(frozen=True)
class _JobOutcome[T]:
    value: T | None = None
    error: Exception | None = None


class _TextProcessorExecutorOwner:
    """Own one dedicated executor and every submitted conversion until settled.

    Closing permanently rejects new work, joins all jobs that crossed the gate,
    and only then shuts down the pool.  Each request waits through
    :func:`join_task`, so cancelling the coroutine cannot detach the physical
    conversion thread or let app shutdown race past its late side effects.
    """

    def __init__(
        self,
        executor_factory: Callable[[], ThreadPoolExecutor] | None = None,
    ) -> None:
        self._executor_factory = executor_factory or (
            lambda: ThreadPoolExecutor(
                max_workers=_MAX_WORKERS,
                thread_name_prefix="text-process",
            )
        )
        self._executor: ThreadPoolExecutor | None = None
        self._executor_physically_closed = False
        self._jobs: set[asyncio.Task[Any]] = set()
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
        return len(self._jobs)

    @property
    def has_executor(self) -> bool:
        return self._executor is not None

    def _get_executor(self) -> ThreadPoolExecutor:
        if self._closing:
            raise RuntimeError("Text processor is shutting down")
        executor = self._executor
        if executor is None:
            executor = self._executor_factory()
            self._executor = executor
            self._executor_physically_closed = False
        return executor

    @staticmethod
    async def _execute[T](
        executor: ThreadPoolExecutor,
        invocation: Callable[[], T],
    ) -> _JobOutcome[T]:
        loop = asyncio.get_running_loop()
        try:
            value = await loop.run_in_executor(executor, invocation)
        except Exception as error:
            return _JobOutcome(error=error)
        return _JobOutcome(value=value)

    async def run[T, **P](
        self,
        func: Callable[P, T],
        /,
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> T:
        executor = self._get_executor()
        invocation: Callable[[], T] = partial(func, *args, **kwargs)
        task = create_lifecycle_task(
            self._execute(executor, invocation),
            name="text-process-job",
        )
        self._jobs.add(task)
        try:
            outcome = await join_task(task)
        finally:
            self._jobs.discard(task)
        if outcome.error is not None:
            raise outcome.error
        return cast(T, outcome.value)

    async def _finalize(self) -> None:
        # The permanent gate was closed before this task was created, so this
        # snapshot contains every job that could have submitted to the executor.
        # Conversion failures belong to their request waiter; shutdown only needs
        # to prove the physical thread settled before releasing the pool.
        jobs = tuple(self._jobs)
        errors: list[BaseException] = []
        for job in jobs:
            try:
                await join_task(job)
            except BaseException as error:  # noqa: BLE001 — settle every physical job
                errors.append(error)
        self._jobs.difference_update(jobs)

        executor = self._executor
        if executor is not None and not self._executor_physically_closed:
            # Every submitted future has settled, but worker-thread retirement is
            # still blocking work.  ``run_in_thread`` owns and physically joins the
            # shutdown invocation, so teardown neither blocks the event loop nor
            # detaches a second, unobserved lifecycle.
            try:
                await run_in_thread(
                    executor.shutdown,
                    wait=True,
                    cancel_futures=False,
                )
            except BaseException as error:  # noqa: BLE001 — preserve retryable close debt
                errors.append(error)
            else:
                # Record physical teardown immediately.  A job lifecycle error
                # raised below must not make a retry invoke shutdown twice on an
                # executor whose worker threads are already gone.
                self._executor_physically_closed = True
        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise BaseExceptionGroup("Text processor jobs did not settle cleanly", errors)

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closing = True
        task = self._shutdown_task
        if task is None:
            task = create_lifecycle_task(
                self._finalize(),
                name="text-processor-shutdown",
            )
            self._shutdown_task = task

        try:
            await join_task(task)
        finally:
            if self._shutdown_task is task and task.done():
                if not task.cancelled() and task.exception() is None:
                    self._executor = None
                    self._closed = True
                # A failed shutdown keeps the gate closed and the executor
                # strongly owned; the next aclose() retries the same debt.
                self._shutdown_task = None


_owner: _TextProcessorExecutorOwner | None = None


def _get_owner() -> _TextProcessorExecutorOwner:
    global _owner
    if _owner is None:
        _owner = _TextProcessorExecutorOwner()
    return _owner


async def startup_text_processor() -> None:
    """Install a fresh worker-pool generation after retiring any prior one."""
    global _owner
    previous = _owner
    if previous is not None:
        await previous.aclose()
    _owner = _TextProcessorExecutorOwner()


async def shutdown_text_processor() -> None:
    """Close the current generation; failed shutdown remains retryable."""
    global _owner
    owner = _owner
    if owner is None:
        return
    await owner.aclose()
    if _owner is owner and owner.closed:
        _owner = None


def _get_hanlo() -> TaigiConverter:
    global _hanlo_converter
    if _hanlo_converter is None:
        with _init_lock:
            if _hanlo_converter is None:  # double-checked: re-verify after acquiring the lock
                _hanlo_converter = TaigiConverter()
    return _hanlo_converter


def _get_taibun() -> TaibunConverter:
    global _taibun_converter
    if _taibun_converter is None:
        with _init_lock:
            if _taibun_converter is None:  # double-checked: re-verify after acquiring the lock
                # system="Tailo" = 台羅拼音；format="number" = 數字調號（Piper TTS 慣用格式）
                _taibun_converter = TaibunConverter(system="Tailo", format="number")
    return _taibun_converter


@dataclass
class TextProcessResult:
    hanlo: str  # 漢羅混合文字（中間產物，可用於 debug / admin 追蹤）
    tailo: str  # 台羅拼音（送 TTS）


def process(zh_text: str) -> TextProcessResult:
    """Convert Mandarin Chinese to Tailo romanization via 漢羅.

    Returns empty strings on empty input. Propagates conversion errors to caller.
    """
    if not zh_text.strip():
        return TextProcessResult(hanlo="", tailo="")

    hanlo = str(_get_hanlo().convert(zh_text))
    tailo = str(_get_taibun().get(hanlo)) if hanlo else ""
    return TextProcessResult(hanlo=hanlo, tailo=tailo)


async def process_async(zh_text: str) -> TextProcessResult:
    """Run CPU-bound conversion in the current app generation's owned pool."""
    return await _get_owner().run(process, zh_text)
