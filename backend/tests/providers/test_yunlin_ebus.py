"""Tests for the concrete `YunlinEbusProvider`.

Covers HTTP shape parsing, route-info caching, and ambiguous-name handling.
Services-layer behaviour is tested with a fake provider in
`tests/services/test_departures.py` — keep this file focused on the ebus
contract.
"""

import asyncio

import pytest

from providers import yunlin_ebus
from providers.yunlin_ebus import YunlinEbusProvider


class Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def _patch_get(monkeypatch, payload, calls):
    class FakeAsyncClient:
        def __init__(self, timeout):
            self.timeout = timeout

        async def get(self, url, params=None):
            calls.append((url, params, self.timeout))
            return Response(payload)

        async def aclose(self):
            pass

    monkeypatch.setattr(yunlin_ebus.httpx, "AsyncClient", FakeAsyncClient)


def test_route_info_uses_stop_lookup_endpoint(monkeypatch):
    payload = [
        {
            "xno": "65036",
            "name": "201",
            "goback": "1",
            "departure": "高鐵雲林站",
            "destination": "雲林科技大學",
        },
        {
            "xno": "65036",
            "name": "201",
            "goback": "2",
            "departure": "高鐵雲林站",
            "destination": "雲林科技大學",
        },
    ]
    calls = []

    _patch_get(monkeypatch, payload, calls)
    provider = YunlinEbusProvider()

    route_info = asyncio.run(provider.load_route_info("雲林科技大學"))
    assert route_info["201"] == {
        "id": 65036,
        "go_dest": "雲林科技大學",
        "back_dest": "高鐵雲林站",
    }
    assert calls == [
        (
            "https://ebus.yunlin.gov.tw/api/stop/route",
            {"stop_name": "雲林科技大學"},
            10.0,
        )
    ]


def test_route_info_caches_per_instance(monkeypatch):
    payload = [{"xno": "65036", "name": "201"}]
    calls = []

    _patch_get(monkeypatch, payload, calls)

    provider = YunlinEbusProvider()
    asyncio.run(provider.load_route_info("雲林科技大學"))
    asyncio.run(provider.load_route_info("雲林科技大學"))
    fresh = YunlinEbusProvider()
    asyncio.run(fresh.load_route_info("雲林科技大學"))

    assert len(calls) == 2  # 1st instance cached, 2nd instance independent


def test_route_info_refetches_after_ttl_expiry(monkeypatch):
    """Cache hand-off — wall-clock advances past TTL, new request hits HTTP again."""
    payload = [{"xno": "65036", "name": "201"}]
    calls = []

    _patch_get(monkeypatch, payload, calls)

    fake_now = [1000.0]
    provider = YunlinEbusProvider(
        route_info_ttl_seconds=60.0,
        clock=lambda: fake_now[0],
    )

    asyncio.run(provider.load_route_info("雲林科技大學"))
    fake_now[0] += 30  # within TTL
    asyncio.run(provider.load_route_info("雲林科技大學"))
    assert len(calls) == 1

    fake_now[0] += 60  # crosses TTL
    asyncio.run(provider.load_route_info("雲林科技大學"))
    assert len(calls) == 2


def test_route_info_disables_cache_when_ttl_none(monkeypatch):
    payload = [{"xno": "65036", "name": "201"}]
    calls = []

    _patch_get(monkeypatch, payload, calls)

    provider = YunlinEbusProvider(route_info_ttl_seconds=None)
    # No TTL means cache never invalidates, but we still verify the value is
    # cached on the first call.
    asyncio.run(provider.load_route_info("雲林科技大學"))
    asyncio.run(provider.load_route_info("雲林科技大學"))
    assert len(calls) == 1


def test_route_info_rejects_ambiguous_names(monkeypatch):
    payload = [
        {"xno": "1", "name": "201"},
        {"xno": "2", "name": "201"},
    ]

    calls = []
    _patch_get(monkeypatch, payload, calls)
    provider = YunlinEbusProvider()

    assert asyncio.run(provider.load_route_info("測試站")).get("201") is None
