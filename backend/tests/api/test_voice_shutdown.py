"""Process-lifespan shutdown tests for the authoritative voice owners."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

import api.voice as voice_module
from providers.cloudflare_turn import TurnIceServers


@pytest.fixture(autouse=True)
def _fresh_voice_runtime():
    asyncio.run(voice_module.shutdown())
    voice_module.startup()
    yield
    asyncio.run(voice_module.shutdown())


def test_shutdown_cancels_and_joins_active_pipeline_tasks():
    async def run() -> None:
        cancellation_started = asyncio.Event()
        cancellation_finished = asyncio.Event()

        async def active_pipeline() -> None:
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cancellation_started.set()
                await asyncio.sleep(0)
                cancellation_finished.set()
                raise

        runtime = voice_module._require_voice_runtime()
        task = await runtime.pipeline_tasks.start(
            active_pipeline(),
            name="active-pipeline",
        )
        await voice_module.shutdown()

        assert task.cancelled()
        assert cancellation_started.is_set()
        assert cancellation_finished.is_set()
        assert runtime.pipeline_tasks.closed
        assert runtime.pipeline_tasks.owned_count == 0
        assert runtime.shutdown_task is None
        assert runtime.closed

    asyncio.run(run())


def test_concurrent_shutdown_waiters_join_one_cancellation_safe_teardown():
    async def run() -> None:
        close_started = asyncio.Event()
        allow_close = asyncio.Event()
        close_calls = 0

        async def close_voice() -> None:
            nonlocal close_calls
            close_calls += 1
            close_started.set()
            await allow_close.wait()

        runtime = voice_module._require_voice_runtime()
        with patch.object(runtime, "_finalize", close_voice):
            first = asyncio.create_task(voice_module.shutdown())
            await close_started.wait()
            second = asyncio.create_task(voice_module.shutdown())

            first.cancel()
            await asyncio.sleep(0)
            assert not first.done()
            assert not second.done()

            allow_close.set()
            with pytest.raises(asyncio.CancelledError):
                await first
            await second

        assert close_calls == 1
        assert runtime.shutdown_task is None
        assert runtime.closed

    asyncio.run(run())


def test_failed_shutdown_releases_task_identity_and_retries_same_closed_gate():
    async def run() -> None:
        calls = 0

        async def flaky_shutdown() -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("shutdown failed")

        runtime = voice_module._require_voice_runtime()
        with patch.object(runtime, "_finalize", flaky_shutdown):
            with pytest.raises(RuntimeError, match="shutdown failed"):
                await voice_module.shutdown()
            assert runtime.shutdown_task is None
            assert not runtime.closed
            assert runtime.closing

            await voice_module.shutdown()

        assert calls == 2
        assert runtime.shutdown_task is None
        assert runtime.closed

    asyncio.run(run())


def test_successful_shutdown_is_terminal_without_redundant_teardown_task():
    async def run() -> None:
        runtime = voice_module._require_voice_runtime()
        close_voice = AsyncMock()
        with patch.object(runtime, "_finalize", close_voice):
            await voice_module.shutdown()
            await voice_module.shutdown()

        close_voice.assert_awaited_once()
        assert runtime.shutdown_task is None
        assert runtime.closed

    asyncio.run(run())


def test_shutdown_joins_inflight_turn_operation_and_blocks_cross_generation_mutation():
    async def run() -> None:
        runtime = voice_module._require_voice_runtime()
        fetch_started = asyncio.Event()
        allow_fetch = asyncio.Event()

        async def blocked_turn_fetch() -> TurnIceServers:
            fetch_started.set()
            await allow_fetch.wait()
            return TurnIceServers(browser=(), aiortc=())

        with (
            patch.object(voice_module, "get_turn_ice_servers", blocked_turn_fetch),
            patch.object(runtime.handler, "update_ice_servers") as update_ice_servers,
        ):
            request = asyncio.create_task(voice_module.webrtc_ice_servers())
            await fetch_started.wait()
            assert runtime.active_operation_count == 1

            closing = asyncio.create_task(voice_module.shutdown())
            while not runtime.closing:
                await asyncio.sleep(0)
            await asyncio.sleep(0)

            assert not closing.done()
            with pytest.raises(RuntimeError, match="before shutdown succeeds"):
                voice_module.startup()

            allow_fetch.set()
            with pytest.raises(HTTPException) as excinfo:
                await request
            assert excinfo.value.status_code == 503
            update_ice_servers.assert_not_called()

            await closing

        assert runtime.active_operation_count == 0
        assert runtime.closed
        assert voice_module._voice_runtime is runtime

        voice_module.startup()
        successor = voice_module._require_voice_runtime()
        assert successor is not runtime
        assert not successor.closing

    asyncio.run(run())
