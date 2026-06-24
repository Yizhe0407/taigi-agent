"""Tests for the TdxBusProvider.

Covers token caching, response normalisation, route-info construction,
and estimate caching. HTTP is monkey-patched so no real network calls occur.
"""

from __future__ import annotations

import asyncio

from providers import tdx_bus
from providers.tdx_bus import TdxBusProvider


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _patch_http(monkeypatch, token_resp: dict, api_resp):
    """Replace the shared httpx client with a fake that returns preset payloads."""

    class FakeClient:
        async def post(self, url, **kwargs):
            return _FakeResp(token_resp)

        async def get(self, url, **kwargs):
            payload = api_resp() if callable(api_resp) else api_resp
            return _FakeResp(payload)

    monkeypatch.setattr(tdx_bus, "get_http_client", lambda: FakeClient())


_TOKEN = {"access_token": "fake-token", "expires_in": 3600}

_STOP_OF_ROUTE = [
    {
        "SubRouteName": {"Zh_tw": "201"},
        "Direction": 0,
        "Stops": [
            {"StopSequence": 1, "StopName": {"Zh_tw": "高鐵雲林站"}},
            {"StopSequence": 2, "StopName": {"Zh_tw": "雲林科技大學"}},
        ],
    },
    {
        "SubRouteName": {"Zh_tw": "201"},
        "Direction": 1,
        "Stops": [
            {"StopSequence": 1, "StopName": {"Zh_tw": "雲林科技大學"}},
            {"StopSequence": 2, "StopName": {"Zh_tw": "高鐵雲林站"}},
        ],
    },
]

_ETA_ROWS = [
    {"SubRouteName": {"Zh_tw": "201"}, "Direction": 0, "StopStatus": 0, "EstimateTime": 300},
    {"SubRouteName": {"Zh_tw": "201"}, "Direction": 1, "StopStatus": 1, "EstimateTime": None},
]


def test_load_route_info_builds_terminals(monkeypatch):
    _patch_http(monkeypatch, _TOKEN, _STOP_OF_ROUTE)
    provider = TdxBusProvider("id", "secret")
    info = asyncio.run(provider.load_route_info("雲林科技大學"))
    assert "201" in info
    assert info["201"]["id"] == "201"
    assert info["201"]["go_dest"] == "雲林科技大學"
    assert info["201"]["back_dest"] == "高鐵雲林站"


def test_load_route_info_caches_per_stop(monkeypatch):
    calls = []

    class CountingClient:
        async def post(self, url, **kwargs):
            return _FakeResp(_TOKEN)

        async def get(self, url, **kwargs):
            calls.append(url)
            return _FakeResp(_STOP_OF_ROUTE)

    monkeypatch.setattr(tdx_bus, "get_http_client", lambda: CountingClient())
    provider = TdxBusProvider("id", "secret")

    asyncio.run(provider.load_route_info("雲林科技大學"))
    asyncio.run(provider.load_route_info("雲林科技大學"))  # cache hit

    # Two parallel calls (City + InterCity) on first fetch, none on second.
    assert len(calls) == 2


def test_load_route_info_refetches_after_ttl(monkeypatch):
    calls = []

    class CountingClient:
        async def post(self, url, **kwargs):
            return _FakeResp(_TOKEN)

        async def get(self, url, **kwargs):
            calls.append(url)
            return _FakeResp(_STOP_OF_ROUTE)

    monkeypatch.setattr(tdx_bus, "get_http_client", lambda: CountingClient())
    fake_now = [1000.0]
    provider = TdxBusProvider("id", "secret", route_info_ttl_seconds=60.0, clock=lambda: fake_now[0])

    asyncio.run(provider.load_route_info("雲林科技大學"))
    assert len(calls) == 2  # City + InterCity

    fake_now[0] += 30  # within TTL
    asyncio.run(provider.load_route_info("雲林科技大學"))
    assert len(calls) == 2  # still cached

    fake_now[0] += 60  # past TTL
    asyncio.run(provider.load_route_info("雲林科技大學"))
    assert len(calls) == 4  # re-fetched


def test_fetch_eta_at_stop_normalises_fields(monkeypatch):
    _patch_http(monkeypatch, _TOKEN, _ETA_ROWS)
    provider = TdxBusProvider("id", "secret")
    rows = asyncio.run(provider.fetch_eta_at_stop("雲林科技大學"))

    # City and InterCity both return the same _ETA_ROWS (dir=0 + dir=1).
    # After dedup by min sequence, 2 unique (sub_route_name, direction) rows remain.
    assert len(rows) == 2
    r = next(r for r in rows if r["direction"] == 0)
    assert r["sub_route_name"] == "201"
    assert r["stop_status"] == 0
    assert r["estimate_seconds"] == 300


def test_fetch_route_estimate_caches_within_ttl(monkeypatch):
    calls = []

    class CountingClient:
        async def post(self, url, **kwargs):
            return _FakeResp(_TOKEN)

        async def get(self, url, **kwargs):
            calls.append(url)
            return _FakeResp(_ETA_ROWS)

    monkeypatch.setattr(tdx_bus, "get_http_client", lambda: CountingClient())
    fake_now = [0.0]
    provider = TdxBusProvider("id", "secret", route_estimate_ttl_seconds=10.0, clock=lambda: fake_now[0])

    asyncio.run(provider.fetch_route_estimate("201"))
    fetch1 = len(calls)

    fake_now[0] += 5  # within TTL
    asyncio.run(provider.fetch_route_estimate("201"))
    assert len(calls) == fetch1  # served from cache


def test_fetch_route_estimate_refetches_after_ttl(monkeypatch):
    calls = []

    class CountingClient:
        async def post(self, url, **kwargs):
            return _FakeResp(_TOKEN)

        async def get(self, url, **kwargs):
            calls.append(url)
            return _FakeResp(_ETA_ROWS)

    monkeypatch.setattr(tdx_bus, "get_http_client", lambda: CountingClient())
    fake_now = [0.0]
    provider = TdxBusProvider("id", "secret", route_estimate_ttl_seconds=10.0, clock=lambda: fake_now[0])

    asyncio.run(provider.fetch_route_estimate("201"))
    fetch1 = len(calls)

    fake_now[0] += 11  # past TTL
    asyncio.run(provider.fetch_route_estimate("201"))
    assert len(calls) > fetch1  # re-fetched


def test_token_is_cached(monkeypatch):
    post_calls = []

    class CountingClient:
        async def post(self, url, **kwargs):
            post_calls.append(url)
            return _FakeResp(_TOKEN)

        async def get(self, url, **kwargs):
            return _FakeResp([])

    monkeypatch.setattr(tdx_bus, "get_http_client", lambda: CountingClient())
    provider = TdxBusProvider("id", "secret")

    asyncio.run(provider.fetch_eta_at_stop("A"))
    asyncio.run(provider.fetch_eta_at_stop("B"))

    assert len(post_calls) == 1  # token reused


def test_fetch_routes_at_stop_deduplicates(monkeypatch):
    _patch_http(monkeypatch, _TOKEN, _STOP_OF_ROUTE)
    provider = TdxBusProvider("id", "secret")
    routes = asyncio.run(provider.fetch_routes_at_stop("雲林科技大學"))
    names = [r["sub_route_name"] for r in routes]
    # 201 appears in both City and InterCity response, and both directions — only once in output
    assert names.count("201") == 1


def test_fetch_route_estimate_city_uses_single_endpoint(monkeypatch):
    """City route (201) should only query the City endpoint — not InterCity."""
    calls = []

    class CountingClient:
        async def post(self, url, **kwargs):
            return _FakeResp(_TOKEN)

        async def get(self, url, **kwargs):
            calls.append(url)
            return _FakeResp(_ETA_ROWS)

    monkeypatch.setattr(tdx_bus, "get_http_client", lambda: CountingClient())
    provider = TdxBusProvider("id", "secret")
    asyncio.run(provider.fetch_route_estimate("201"))

    assert len(calls) == 1
    assert "City" in calls[0]


def test_fetch_route_estimate_intercity_uses_single_endpoint(monkeypatch):
    """Intercity route (7123A) should only query the InterCity endpoint."""
    calls = []

    class CountingClient:
        async def post(self, url, **kwargs):
            return _FakeResp(_TOKEN)

        async def get(self, url, **kwargs):
            calls.append(url)
            return _FakeResp(_ETA_ROWS)

    monkeypatch.setattr(tdx_bus, "get_http_client", lambda: CountingClient())
    provider = TdxBusProvider("id", "secret")
    asyncio.run(provider.fetch_route_estimate("7123A"))

    assert len(calls) == 1
    assert "InterCity" in calls[0]


def test_fetch_eta_at_stop_degrades_when_one_endpoint_fails(monkeypatch):
    """If one endpoint returns 429/error, results from the other are still used."""
    import httpx

    class PartialClient:
        async def post(self, url, **kwargs):
            return _FakeResp(_TOKEN)

        async def get(self, url, **kwargs):
            if "InterCity" in url:
                raise httpx.HTTPStatusError("429", request=None, response=None)
            return _FakeResp(_ETA_ROWS)

    monkeypatch.setattr(tdx_bus, "get_http_client", lambda: PartialClient())
    provider = TdxBusProvider("id", "secret")
    rows = asyncio.run(provider.fetch_eta_at_stop("雲林科技大學"))
    # City returned 2 rows, InterCity failed — should still get city rows
    assert len(rows) == 2


def test_fetch_eta_at_stop_deduplicates_circular_route(monkeypatch):
    """Circular routes return two rows for the kiosk stop; keep the lower-sequence one."""
    circular_eta = [
        # seq=1: departure point — 末班已過
        {"SubRouteName": {"Zh_tw": "Y02"}, "Direction": 0, "StopStatus": 3, "EstimateTime": None, "StopSequence": 1},
        # seq=10: arrival point — 7.5h away (irrelevant for boarding)
        {"SubRouteName": {"Zh_tw": "Y02"}, "Direction": 0, "StopStatus": 0, "EstimateTime": 27000, "StopSequence": 10},
    ]

    class FixedClient:
        async def post(self, url, **kwargs):
            return _FakeResp(_TOKEN)

        async def get(self, url, **kwargs):
            if "City" in url:
                return _FakeResp(circular_eta)
            return _FakeResp([])  # InterCity: nothing

    monkeypatch.setattr(tdx_bus, "get_http_client", lambda: FixedClient())
    provider = TdxBusProvider("id", "secret")
    rows = asyncio.run(provider.fetch_eta_at_stop("斗六火車站"))

    y02_rows = [r for r in rows if r["sub_route_name"] == "Y02"]
    assert len(y02_rows) == 1, "should keep only one row per route+direction"
    assert y02_rows[0]["stop_status"] == 3, "should keep seq=1 (末班已過), not seq=10 (7.5h)"
    assert y02_rows[0]["stop_sequence"] == 1


def test_fetch_eta_at_stop_raises_when_both_endpoints_fail(monkeypatch):
    """If both endpoints fail, the exception propagates."""
    import httpx

    class FailingClient:
        async def post(self, url, **kwargs):
            return _FakeResp(_TOKEN)

        async def get(self, url, **kwargs):
            raise httpx.HTTPStatusError("429", request=None, response=None)

    monkeypatch.setattr(tdx_bus, "get_http_client", lambda: FailingClient())
    provider = TdxBusProvider("id", "secret")
    try:
        asyncio.run(provider.fetch_eta_at_stop("雲林科技大學"))
        assert False, "should have raised"
    except Exception as e:
        assert "429" in str(e)
