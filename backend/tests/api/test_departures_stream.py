"""Departures SSE：首推 snapshot、warmup tick 喚醒、錯誤事件。

直接測 `_departure_events` generator——無限 SSE 串流經 TestClient 無法俐落
關閉（generator 卡在 fallback wait），測 generator 本身既快又可控。
"""

import asyncio
import json

import pytest
from fastapi import HTTPException

import api.departures
from api.departures import _departure_events, notify_snapshot_refreshed, stream_departures_here
from services.departures import DepartureSnapshotUnavailable
from tests.api.test_api import _departure_snapshot


def _payload(event: str) -> dict:
    assert event.startswith("data: ") and event.endswith("\n\n")
    return json.loads(event[len("data: ") : -2])


def test_stream_pushes_snapshot_immediately(monkeypatch):
    async def fake_snapshot(*, updated_at=None):
        return _departure_snapshot()

    monkeypatch.setattr(api.departures, "get_departure_snapshot_here", fake_snapshot)

    async def run():
        events = _departure_events()
        try:
            return await anext(events)
        finally:
            await events.aclose()

    payload = _payload(asyncio.run(run()))
    assert payload["stopName"] == "雲林科技大學"
    assert payload["summary"]["availableCount"] == 1
    assert len(payload["routes"]) == 2


def test_stream_unavailable_becomes_error_event(monkeypatch):
    async def unavailable(*, updated_at=None):
        raise DepartureSnapshotUnavailable("上游查詢失敗")

    monkeypatch.setattr(api.departures, "get_departure_snapshot_here", unavailable)

    async def run():
        events = _departure_events()
        try:
            return await anext(events)
        finally:
            await events.aclose()

    assert _payload(asyncio.run(run())) == {"error": "上游查詢失敗"}


def test_notify_wakes_waiting_stream(monkeypatch):
    """notify 後立刻收到第二筆，不必等 fallback timeout。"""
    calls = {"n": 0}

    async def fake_snapshot(*, updated_at=None):
        calls["n"] += 1
        return _departure_snapshot()

    monkeypatch.setattr(api.departures, "get_departure_snapshot_here", fake_snapshot)
    # Fallback 拉長到 30 s：測試若靠 timeout 而非 notify 取得第二筆會直接超時失敗。
    monkeypatch.setattr(api.departures, "_STREAM_FALLBACK_SECONDS", 30.0)

    async def run():
        events = _departure_events()
        try:
            await anext(events)  # initial push
            second = asyncio.ensure_future(anext(events))
            await asyncio.sleep(0.01)  # let the generator park on the tick event
            notify_snapshot_refreshed()
            return await asyncio.wait_for(second, timeout=2)
        finally:
            await events.aclose()

    payload = _payload(asyncio.run(run()))
    assert payload["stopName"] == "雲林科技大學"
    assert calls["n"] == 2


# ── Concurrent-connection admission control ────────────────────────────────


def test_stream_rejects_over_limit_connection(monkeypatch):
    """At capacity, the endpoint raises 503 and takes no slot for the attempt."""
    monkeypatch.setattr(api.departures, "_MAX_STREAM_CONNECTIONS", 1)
    monkeypatch.setattr(api.departures, "_active_stream_connections", 1)

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(stream_departures_here())

    assert excinfo.value.status_code == 503
    assert api.departures._active_stream_connections == 1


def test_stream_slot_released_on_disconnect_and_reused(monkeypatch):
    """A connection's slot is taken only once actually streamed, and frees on
    disconnect so the next connection can succeed."""
    monkeypatch.setattr(api.departures, "_MAX_STREAM_CONNECTIONS", 1)
    monkeypatch.setattr(api.departures, "_active_stream_connections", 0)

    async def fake_snapshot(*, updated_at=None):
        return _departure_snapshot()

    monkeypatch.setattr(api.departures, "get_departure_snapshot_here", fake_snapshot)

    async def run():
        response = await stream_departures_here()
        # Constructing the response only passed the capacity check — no slot
        # is held until the generator body actually starts running.
        assert api.departures._active_stream_connections == 0

        await anext(response.body_iterator)  # starts the generator, takes the slot
        assert api.departures._active_stream_connections == 1

        # A second connection is rejected while the first is still open.
        with pytest.raises(HTTPException) as excinfo:
            await stream_departures_here()
        assert excinfo.value.status_code == 503

        # Client disconnects: Starlette closes the body iterator.
        await response.body_iterator.aclose()
        assert api.departures._active_stream_connections == 0

        # The freed slot lets a new connection through.
        second_response = await stream_departures_here()
        await anext(second_response.body_iterator)
        assert api.departures._active_stream_connections == 1
        await second_response.body_iterator.aclose()
        assert api.departures._active_stream_connections == 0

    asyncio.run(run())


def test_stream_slot_released_on_generator_exception(monkeypatch):
    """An exception raised while building a snapshot still frees the slot."""
    monkeypatch.setattr(api.departures, "_MAX_STREAM_CONNECTIONS", 1)
    monkeypatch.setattr(api.departures, "_active_stream_connections", 0)

    async def boom(*, updated_at=None):
        raise RuntimeError("unexpected failure")

    monkeypatch.setattr(api.departures, "get_departure_snapshot_here", boom)

    async def run():
        response = await stream_departures_here()
        assert api.departures._active_stream_connections == 0  # not started yet

        with pytest.raises(RuntimeError):
            await anext(response.body_iterator)  # starts, acquires, raises, releases

        assert api.departures._active_stream_connections == 0

    asyncio.run(run())


def test_stream_never_iterated_connection_does_not_leak_slot(monkeypatch):
    """Regression: a `StreamingResponse` whose body iterator is closed
    without ever being iterated (the shape Starlette produces on an
    immediate client disconnect between sending headers and the first
    `__anext__`) must not leave a slot held. This fails against a design
    that acquires the slot in the endpoint function and only releases it in
    the generator's `finally` — an async generator's body never starts
    running if `aclose()` is called before the first `__anext__`, so that
    `finally` never fires and the slot leaks permanently.
    """
    monkeypatch.setattr(api.departures, "_MAX_STREAM_CONNECTIONS", 1)
    monkeypatch.setattr(api.departures, "_active_stream_connections", 0)

    async def fake_snapshot(*, updated_at=None):
        return _departure_snapshot()

    monkeypatch.setattr(api.departures, "get_departure_snapshot_here", fake_snapshot)

    async def run():
        response = await stream_departures_here()
        # Never call anext() — simulate the client vanishing before the
        # generator body ever started executing.
        await response.body_iterator.aclose()
        assert api.departures._active_stream_connections == 0

        # No leaked slot means a fresh connection still succeeds.
        second_response = await stream_departures_here()
        await anext(second_response.body_iterator)
        assert api.departures._active_stream_connections == 1
        await second_response.body_iterator.aclose()

    asyncio.run(run())
