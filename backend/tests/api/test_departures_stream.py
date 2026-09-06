"""Departure SSE generation ownership, notification, and shutdown tests."""

import asyncio
import json
from contextlib import suppress

import pytest
from fastapi import HTTPException

import api.departures
from api.departures import (
    _DepartureStreamCapacityError,
    notify_snapshot_refreshed,
    shutdown_departure_streams,
    startup_departure_streams,
    stream_departures_here,
)
from services.departures import DepartureSnapshotUnavailable
from tests.api.test_api import _departure_snapshot


def _payload(event: str) -> dict:
    assert event.startswith("data: ") and event.endswith("\n\n")
    return json.loads(event[len("data: ") : -2])


async def _startup_runtime() -> api.departures._DepartureStreamRuntime:
    await startup_departure_streams()
    runtime = api.departures._departure_runtime
    assert runtime is not None
    return runtime


def test_stream_pushes_snapshot_immediately(monkeypatch):
    async def fake_snapshot(*, updated_at=None):
        return _departure_snapshot()

    monkeypatch.setattr(api.departures, "get_departure_snapshot_here", fake_snapshot)

    async def run() -> str:
        await _startup_runtime()
        response = await stream_departures_here()
        try:
            return await anext(response.body_iterator)
        finally:
            await response.body_iterator.aclose()
            await shutdown_departure_streams()

    payload = _payload(asyncio.run(run()))
    assert payload["stopName"] == "雲林科技大學"
    assert payload["summary"]["availableCount"] == 1
    assert len(payload["routes"]) == 2


def test_stream_unavailable_becomes_error_event(monkeypatch):
    async def unavailable(*, updated_at=None):
        raise DepartureSnapshotUnavailable("上游查詢失敗")

    monkeypatch.setattr(api.departures, "get_departure_snapshot_here", unavailable)

    async def run() -> str:
        await _startup_runtime()
        response = await stream_departures_here()
        try:
            return await anext(response.body_iterator)
        finally:
            await response.body_iterator.aclose()
            await shutdown_departure_streams()

    assert _payload(asyncio.run(run())) == {"error": "上游查詢失敗"}


def test_notify_wakes_only_the_current_generation(monkeypatch):
    calls = 0

    async def fake_snapshot(*, updated_at=None):
        nonlocal calls
        calls += 1
        return _departure_snapshot()

    monkeypatch.setattr(api.departures, "get_departure_snapshot_here", fake_snapshot)

    async def run() -> str:
        await _startup_runtime()
        response = await stream_departures_here()
        second: asyncio.Task[str] | None = None
        try:
            await anext(response.body_iterator)
            second = asyncio.create_task(anext(response.body_iterator))
            await asyncio.sleep(0)  # drive the generator onto its captured refresh event
            assert not second.done()

            notify_snapshot_refreshed()
            for _ in range(3):
                await asyncio.sleep(0)
            assert second.done(), "current-generation notification did not wake the stream"
            return second.result()
        finally:
            if second is not None and not second.done():
                second.cancel()
                with suppress(asyncio.CancelledError):
                    await second
            await response.body_iterator.aclose()
            await shutdown_departure_streams()

    payload = _payload(asyncio.run(run()))
    assert payload["stopName"] == "雲林科技大學"
    assert calls == 2


def test_capacity_is_authoritative_at_first_iteration_and_reusable(monkeypatch):
    monkeypatch.setattr(api.departures, "_MAX_STREAM_CONNECTIONS", 1)

    async def fake_snapshot(*, updated_at=None):
        return _departure_snapshot()

    monkeypatch.setattr(api.departures, "get_departure_snapshot_here", fake_snapshot)

    async def run() -> None:
        runtime = await _startup_runtime()
        first = await stream_departures_here()
        raced = await stream_departures_here()
        try:
            assert runtime.active_count == 0
            await anext(first.body_iterator)
            assert runtime.active_count == 1

            with pytest.raises(HTTPException) as captured:
                await stream_departures_here()
            assert captured.value.status_code == 503

            # Both responses passed the pre-header advisory check, but the
            # first-iteration gate still enforces the hard runtime cap.
            with pytest.raises(_DepartureStreamCapacityError):
                await anext(raced.body_iterator)
            assert runtime.active_count == 1

            await first.body_iterator.aclose()
            assert runtime.active_count == 0

            reused = await stream_departures_here()
            await anext(reused.body_iterator)
            assert runtime.active_count == 1
            await reused.body_iterator.aclose()
            assert runtime.active_count == 0
        finally:
            await first.body_iterator.aclose()
            await raced.body_iterator.aclose()
            await shutdown_departure_streams()

    asyncio.run(run())


def test_never_iterated_response_never_acquires_a_lease(monkeypatch):
    monkeypatch.setattr(api.departures, "_MAX_STREAM_CONNECTIONS", 1)

    async def fake_snapshot(*, updated_at=None):
        return _departure_snapshot()

    monkeypatch.setattr(api.departures, "get_departure_snapshot_here", fake_snapshot)

    async def run() -> None:
        runtime = await _startup_runtime()
        response = await stream_departures_here()
        try:
            await response.body_iterator.aclose()
            assert runtime.active_count == 0

            replacement = await stream_departures_here()
            await anext(replacement.body_iterator)
            assert runtime.active_count == 1
            await replacement.body_iterator.aclose()
            assert runtime.active_count == 0
        finally:
            await response.body_iterator.aclose()
            await shutdown_departure_streams()

    asyncio.run(run())


def test_generator_failure_releases_its_runtime_lease(monkeypatch):
    async def boom(*, updated_at=None):
        raise RuntimeError("unexpected failure")

    monkeypatch.setattr(api.departures, "get_departure_snapshot_here", boom)

    async def run() -> None:
        runtime = await _startup_runtime()
        response = await stream_departures_here()
        try:
            with pytest.raises(RuntimeError, match="unexpected failure"):
                await anext(response.body_iterator)
            assert runtime.active_count == 0
        finally:
            await response.body_iterator.aclose()
            await shutdown_departure_streams()

    asyncio.run(run())


def test_shutdown_closes_gate_waits_for_stream_and_blocks_late_notify(monkeypatch):
    async def fake_snapshot(*, updated_at=None):
        return _departure_snapshot()

    monkeypatch.setattr(api.departures, "get_departure_snapshot_here", fake_snapshot)

    async def run() -> None:
        runtime = await _startup_runtime()
        response = await stream_departures_here()
        await anext(response.body_iterator)
        assert runtime.active_count == 1

        shutdown = asyncio.create_task(shutdown_departure_streams())
        await asyncio.sleep(0)
        assert runtime.closing
        assert not shutdown.done()

        with pytest.raises(HTTPException) as captured:
            await stream_departures_here()
        assert captured.value.status_code == 503

        closed_generation = runtime.capture_refresh()
        notify_snapshot_refreshed()
        assert runtime.capture_refresh() is closed_generation

        with pytest.raises(StopAsyncIteration):
            await anext(response.body_iterator)
        await shutdown

        assert runtime.closed
        assert runtime.active_count == 0
        assert api.departures._departure_runtime is None

        notify_snapshot_refreshed()
        assert api.departures._departure_runtime is None

        await startup_departure_streams()
        replacement = api.departures._departure_runtime
        assert replacement is not None
        assert replacement is not runtime
        await shutdown_departure_streams()

    asyncio.run(run())


def test_cancelled_shutdown_waiter_still_retires_the_physical_generation(monkeypatch):
    async def fake_snapshot(*, updated_at=None):
        return _departure_snapshot()

    monkeypatch.setattr(api.departures, "get_departure_snapshot_here", fake_snapshot)

    async def run() -> None:
        runtime = await _startup_runtime()
        response = await stream_departures_here()
        await anext(response.body_iterator)

        shutdown_waiter = asyncio.create_task(shutdown_departure_streams())
        await asyncio.sleep(0)
        shutdown_waiter.cancel()
        await asyncio.sleep(0)
        assert not shutdown_waiter.done()

        with pytest.raises(StopAsyncIteration):
            await anext(response.body_iterator)
        with pytest.raises(asyncio.CancelledError):
            await shutdown_waiter

        assert runtime.closed
        assert runtime.active_count == 0
        assert api.departures._departure_runtime is None

    asyncio.run(run())
