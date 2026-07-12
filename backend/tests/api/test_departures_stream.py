"""Departures SSE：首推 snapshot、warmup tick 喚醒、錯誤事件。

直接測 `_departure_events` generator——無限 SSE 串流經 TestClient 無法俐落
關閉（generator 卡在 fallback wait），測 generator 本身既快又可控。
"""

import asyncio
import json

import api.departures
from api.departures import _departure_events, notify_snapshot_refreshed
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
