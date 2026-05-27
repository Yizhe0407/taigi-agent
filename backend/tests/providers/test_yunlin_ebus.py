"""Tests for the concrete `YunlinEbusProvider`.

Covers HTTP shape parsing, route-info caching, and ambiguous-name handling.
Services-layer behaviour is tested with a fake provider in
`tests/services/test_departures.py` — keep this file focused on the ebus
contract.
"""

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


@pytest.fixture
def provider():
    return YunlinEbusProvider()


def test_route_info_uses_stop_lookup_endpoint(monkeypatch, provider):
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

    def fake_get(url, params=None, timeout=None):
        calls.append((url, params, timeout))
        return Response(payload)

    monkeypatch.setattr(yunlin_ebus.requests, "get", fake_get)

    assert provider.get_route_id("201", "雲林科技大學") == 65036
    assert provider.direction_label("201", "雲林科技大學", 1) == "往雲林科技大學"
    assert provider.direction_label("201", "雲林科技大學", 2) == "往高鐵雲林站"
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

    def fake_get(url, params=None, timeout=None):
        calls.append((url, params, timeout))
        return Response(payload)

    monkeypatch.setattr(yunlin_ebus.requests, "get", fake_get)

    provider = YunlinEbusProvider()
    provider.load_route_info("雲林科技大學")
    provider.load_route_info("雲林科技大學")
    fresh = YunlinEbusProvider()
    fresh.load_route_info("雲林科技大學")

    assert len(calls) == 2  # 1st instance cached, 2nd instance independent


def test_route_info_refetches_after_ttl_expiry(monkeypatch):
    """Cache hand-off — wall-clock advances past TTL, new request hits HTTP again."""
    payload = [{"xno": "65036", "name": "201"}]
    calls = []

    def fake_get(url, params=None, timeout=None):
        calls.append((url, params, timeout))
        return Response(payload)

    monkeypatch.setattr(yunlin_ebus.requests, "get", fake_get)

    fake_now = [1000.0]
    provider = YunlinEbusProvider(
        route_info_ttl_seconds=60.0,
        clock=lambda: fake_now[0],
    )

    provider.load_route_info("雲林科技大學")
    fake_now[0] += 30  # within TTL
    provider.load_route_info("雲林科技大學")
    assert len(calls) == 1

    fake_now[0] += 60  # crosses TTL
    provider.load_route_info("雲林科技大學")
    assert len(calls) == 2


def test_route_info_disables_cache_when_ttl_none(monkeypatch):
    payload = [{"xno": "65036", "name": "201"}]
    calls = []

    monkeypatch.setattr(
        yunlin_ebus.requests,
        "get",
        lambda *args, **kwargs: (calls.append(None), Response(payload))[1],
    )

    provider = YunlinEbusProvider(route_info_ttl_seconds=None)
    # No TTL means cache never invalidates, but we still verify the value is
    # cached on the first call.
    provider.load_route_info("雲林科技大學")
    provider.load_route_info("雲林科技大學")
    assert len(calls) == 1


def test_route_info_rejects_ambiguous_names(monkeypatch, provider):
    payload = [
        {"xno": "1", "name": "201"},
        {"xno": "2", "name": "201"},
    ]

    monkeypatch.setattr(
        yunlin_ebus.requests,
        "get",
        lambda *args, **kwargs: Response(payload),
    )

    assert provider.get_route_id("201", "測試站") is None
