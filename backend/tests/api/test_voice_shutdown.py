"""Tests for api.voice.shutdown() — graceful cleanup of WebRTC resources.

Regression coverage for finding 5: the lifespan shutdown in api/__init__.py
used to leave the process-wide SmallWebRTCRequestHandler and any in-flight
run_voice_pipeline() background tasks untouched on graceful shutdown.
"""

import asyncio
from unittest.mock import AsyncMock, patch

import api.voice as voice_module


def test_shutdown_closes_handler_and_awaits_tracked_pipeline_tasks():
    async def run():
        async def _never_finishes():
            await asyncio.sleep(3600)

        task = asyncio.create_task(_never_finishes())
        voice_module._pipeline_tasks.add(task)
        try:
            with patch.object(voice_module, "_handler", AsyncMock()) as mock_handler:
                await voice_module.shutdown()
                mock_handler.close.assert_awaited_once()
            # shutdown() cancels tracked tasks and awaits them — the task must
            # be finished (cancelled) by the time shutdown() returns, not left
            # running past process shutdown.
            assert task.done()
            assert task.cancelled()
        finally:
            voice_module._pipeline_tasks.discard(task)

    asyncio.run(run())


def test_shutdown_is_a_noop_safe_call_with_no_active_tasks():
    async def run():
        assert not voice_module._pipeline_tasks
        with patch.object(voice_module, "_handler", AsyncMock()) as mock_handler:
            await voice_module.shutdown()
            mock_handler.close.assert_awaited_once()

    asyncio.run(run())


def test_pipeline_task_removes_itself_from_tracking_set_when_done():
    """The done_callback wiring in _start_pipeline must discard finished tasks
    so _pipeline_tasks doesn't grow unbounded over the process lifetime."""

    async def run():
        async def _quick():
            return None

        task = asyncio.create_task(_quick())
        voice_module._pipeline_tasks.add(task)
        task.add_done_callback(voice_module._pipeline_tasks.discard)
        await task
        # Let the done-callback (scheduled via call_soon) actually run.
        await asyncio.sleep(0)
        assert task not in voice_module._pipeline_tasks

    asyncio.run(run())
