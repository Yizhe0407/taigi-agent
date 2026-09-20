"""Tests for the TaiwanBus eBUS provider adapter."""

from __future__ import annotations

import asyncio

import providers.taiwan_bus as taiwan_bus
from providers.bus import Direction, StopStatus
from providers.taiwan_bus import TaiwanBusProvider


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


_ROUTE_RECORD = {
    "rno": "02010",
    "srno": "201",
    "name": "高鐵雲林站─斗六火車站─雲林科技大學",
}


def _route_rows(outbound: bool) -> list[dict]:
    if outbound:
        return [
            {"na": "高鐵雲林站", "idx": "1", "ptime": "5分", "car": "BUS-1"},
            {"na": "斗六火車站", "idx": "2", "ptime": "3分", "car": "BUS-1"},
            {"na": "雲林科技大學", "idx": "3", "ptime": "末班車駛離", "car": "BUS-1"},
        ]
    return [
        {"na": "雲林科技大學", "idx": "1", "ptime": "2分", "car": "BUS-2"},
        {"na": "斗六火車站", "idx": "2", "ptime": "4分", "car": "BUS-2"},
        {"na": "高鐵雲林站", "idx": "3", "ptime": "6分", "car": "BUS-2"},
    ]


class _FakeClient:
    async def get(self, url, *, params, **kwargs):
        if url.endswith("getData.ashx"):
            assert params["type"] == 1
            query = params["key"]
            if query in {"斗六火車站", "201"}:
                return _FakeResponse([_ROUTE_RECORD])
            return _FakeResponse([])

        assert url.endswith("getRData.ashx")
        key = params["key"]
        if key == "020101":
            return _FakeResponse({"time": "12:00:00", "data": _route_rows(True), "cars": [{"car": "BUS-1"}]})
        if key == "020102":
            return _FakeResponse({"time": "12:00:00", "data": _route_rows(False), "cars": [{"car": "BUS-2"}]})
        return _FakeResponse({"time": "12:00:00", "data": [], "cars": []})


def test_parse_ptime_supports_live_and_schedule_values():
    assert taiwan_bus._parse_ptime("6分") == (StopStatus.AVAILABLE, 360, None)
    assert taiwan_bus._parse_ptime("即將進站") == (StopStatus.AVAILABLE, 0, None)
    assert taiwan_bus._parse_ptime("22:30發車") == (StopStatus.NOT_DEPARTED, None, "22:30")
    assert taiwan_bus._parse_ptime("末班車駛離") == (StopStatus.LAST_DEPARTED, None, None)


def test_fetch_route_estimate_discovers_route_keys_and_normalizes_rows(monkeypatch):
    monkeypatch.setattr(taiwan_bus, "get_http_client", lambda: _FakeClient())
    provider = TaiwanBusProvider()

    rows = asyncio.run(provider.fetch_route_estimate("201"))

    assert rows is not None
    assert len(rows) == 6
    assert {row.direction for row in rows} == {Direction.OUTBOUND, Direction.INBOUND}
    assert rows[1].stop_name == "斗六火車站"
    assert rows[1].eta_seconds == 180
    assert rows[1].vehicle_id == "BUS-1"


def test_load_route_info_filters_route_to_requested_stop(monkeypatch):
    monkeypatch.setattr(taiwan_bus, "get_http_client", lambda: _FakeClient())
    provider = TaiwanBusProvider()

    info = asyncio.run(provider.load_route_info("斗六火車站"))

    assert list(info) == ["201"]
    assert info["201"].outbound_destination == "雲林科技大學"
    assert info["201"].inbound_destination == "高鐵雲林站"


def test_fetch_eta_at_stop_returns_provider_neutral_rows(monkeypatch):
    monkeypatch.setattr(taiwan_bus, "get_http_client", lambda: _FakeClient())
    provider = TaiwanBusProvider()

    rows = asyncio.run(provider.fetch_eta_at_stop("斗六火車站"))

    assert rows is not None
    assert len(rows) == 2
    assert {row.direction for row in rows} == {Direction.OUTBOUND, Direction.INBOUND}
    assert all(row.route_name == "201" for row in rows)
    assert all(row.status is StopStatus.AVAILABLE for row in rows)


def test_unknown_route_returns_none(monkeypatch):
    monkeypatch.setattr(taiwan_bus, "get_http_client", lambda: _FakeClient())
    provider = TaiwanBusProvider()

    assert asyncio.run(provider.fetch_route_estimate("9999")) is None
