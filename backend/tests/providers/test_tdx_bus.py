"""Tests for the TdxBusProvider.

Covers token caching, response normalisation, route-info construction,
estimate caching, and the UID-based ETA path.
"""

from __future__ import annotations

import asyncio

from providers import tdx_bus
from providers.tdx_bus import TdxBusProvider


class _FakeResp:
    def __init__(self, payload, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code
        self.headers: dict = {}

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx

            raise httpx.HTTPStatusError(str(self.status_code), request=None, response=self)

    def json(self):
        return self._payload


def _patch_http(monkeypatch, token_resp: dict, api_resp):
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
            {"StopUID": "YUN-A", "StopSequence": 1, "StopName": {"Zh_tw": "高鐵雲林站"}},
            {"StopUID": "YUN-B", "StopSequence": 2, "StopName": {"Zh_tw": "雲林科技大學"}},
        ],
    },
    {
        "SubRouteName": {"Zh_tw": "201"},
        "Direction": 1,
        "Stops": [
            {"StopUID": "YUN-C", "StopSequence": 1, "StopName": {"Zh_tw": "雲林科技大學"}},
            {"StopUID": "YUN-D", "StopSequence": 2, "StopName": {"Zh_tw": "高鐵雲林站"}},
        ],
    },
]

_ETA_ROWS = [
    {"SubRouteName": {"Zh_tw": "201"}, "Direction": 0, "StopStatus": 0, "EstimateTime": 300},
    {"SubRouteName": {"Zh_tw": "201"}, "Direction": 1, "StopStatus": 1, "EstimateTime": None},
]

# Circular route: kiosk (斗六火車站) is both seq=1 (departure) and seq=10 (arrival).
_CIRCULAR_STOP_OF_ROUTE = [
    {
        "SubRouteName": {"Zh_tw": "Y02"},
        "Direction": 0,
        "Stops": [
            {"StopUID": "YUN-Y02-1", "StopSequence": 1, "StopName": {"Zh_tw": "斗六火車站"}},
            {"StopUID": "YUN-Y02-5", "StopSequence": 5, "StopName": {"Zh_tw": "高鐵雲林站"}},
            {"StopUID": "YUN-Y02-10", "StopSequence": 10, "StopName": {"Zh_tw": "斗六火車站"}},
        ],
    },
]


def test_load_route_info_builds_terminals(monkeypatch):
    _patch_http(monkeypatch, _TOKEN, _STOP_OF_ROUTE)
    provider = TdxBusProvider("id", "secret")
    info = asyncio.run(provider.load_route_info("雲林科技大學"))
    assert "201" in info
    assert info["201"]["id"] == "201"
    assert info["201"]["go_dest"] == "雲林科技大學"
    assert info["201"]["back_dest"] == "高鐵雲林站"


def test_load_route_info_collects_boarding_uids(monkeypatch):
    """After load_route_info, boarding UIDs for the kiosk stop are cached."""
    _patch_http(monkeypatch, _TOKEN, _STOP_OF_ROUTE)
    provider = TdxBusProvider("id", "secret")
    asyncio.run(provider.load_route_info("雲林科技大學"))
    uids = provider._kiosk_uids.get("雲林科技大學")
    # 雲林科技大學 is seq=2 dir=0 (UID=YUN-B) and seq=1 dir=1 (UID=YUN-C)
    assert uids == {"YUN-B", "YUN-C"}


def test_load_route_info_circular_route_captures_first_occurrence(monkeypatch):
    """For a circular route, only the first (seq=1) StopUID is collected, not seq=10."""
    _patch_http(monkeypatch, _TOKEN, _CIRCULAR_STOP_OF_ROUTE)
    provider = TdxBusProvider("id", "secret")
    asyncio.run(provider.load_route_info("斗六火車站"))
    uids = provider._kiosk_uids.get("斗六火車站")
    assert "YUN-Y02-1" in uids, "seq=1 UID should be collected (boarding point)"
    assert "YUN-Y02-10" not in uids, "seq=10 UID must NOT be collected (arrival point)"


def test_fetch_eta_at_stop_uses_uid_filter_when_cached(monkeypatch):
    """When boarding UIDs are cached, fetch_eta_at_stop queries by StopUID."""
    get_params = []

    class TrackingClient:
        async def post(self, url, **kwargs):
            return _FakeResp(_TOKEN)

        async def get(self, url, **kwargs):
            get_params.append(kwargs.get("params", {}))
            return _FakeResp(_ETA_ROWS)

    monkeypatch.setattr(tdx_bus, "get_http_client", lambda: TrackingClient())
    provider = TdxBusProvider("id", "secret")
    # Pre-populate UIDs so the UID path is taken (skip cold-start fallback)
    provider._kiosk_uids["斗六火車站"] = {"YUN-Y02-1", "YUN-Y02-5"}

    asyncio.run(provider.fetch_eta_at_stop("斗六火車站"))

    # Both City and InterCity requests should use StopUID in their filter
    assert all("StopUID" in p.get("$filter", "") for p in get_params)
    assert all("StopName" not in p.get("$filter", "") for p in get_params)


def test_fetch_eta_at_stop_fallback_deduplicates_circular(monkeypatch):
    """Cold-start fallback: name query + min-sequence dedup handles circular routes."""
    circular_eta = [
        {"SubRouteName": {"Zh_tw": "Y02"}, "Direction": 0, "StopStatus": 3, "EstimateTime": None, "StopSequence": 1},
        {"SubRouteName": {"Zh_tw": "Y02"}, "Direction": 0, "StopStatus": 0, "EstimateTime": 27000, "StopSequence": 10},
    ]

    class FixedClient:
        async def post(self, url, **kwargs):
            return _FakeResp(_TOKEN)

        async def get(self, url, **kwargs):
            return _FakeResp(circular_eta if "City" in url else [])

    monkeypatch.setattr(tdx_bus, "get_http_client", lambda: FixedClient())
    provider = TdxBusProvider("id", "secret")
    # No UIDs cached → fallback path
    rows = asyncio.run(provider.fetch_eta_at_stop("斗六火車站"))

    y02 = [r for r in rows if r["sub_route_name"] == "Y02"]
    assert len(y02) == 1
    assert y02[0]["stop_status"] == 3
    assert y02[0]["stop_sequence"] == 1


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
    assert len(calls) == 2  # City + InterCity on first fetch only


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
    assert len(calls) == 2
    fake_now[0] += 30
    asyncio.run(provider.load_route_info("雲林科技大學"))
    assert len(calls) == 2
    fake_now[0] += 60
    asyncio.run(provider.load_route_info("雲林科技大學"))
    assert len(calls) == 4


def test_fetch_eta_at_stop_normalises_fields(monkeypatch):
    _patch_http(monkeypatch, _TOKEN, _ETA_ROWS)
    provider = TdxBusProvider("id", "secret")
    rows = asyncio.run(provider.fetch_eta_at_stop("雲林科技大學"))
    # No UIDs cached → fallback; City + InterCity both return same 2 rows → dedup → 2 unique rows
    assert len(rows) == 2
    r = next(r for r in rows if r["direction"] == 0)
    assert r["sub_route_name"] == "201"
    assert r["stop_status"] == 0
    assert r["estimate_seconds"] == 300


def test_fetch_eta_at_stop_caches_within_ttl(monkeypatch):
    """fetch_eta_at_stop should not re-fetch TDX within the ETA TTL window."""
    calls = []

    class CountingClient:
        async def post(self, url, **kwargs):
            return _FakeResp(_TOKEN)

        async def get(self, url, **kwargs):
            calls.append(url)
            return _FakeResp(_ETA_ROWS)

    monkeypatch.setattr(tdx_bus, "get_http_client", lambda: CountingClient())
    fake_now = [0.0]
    provider = TdxBusProvider("id", "secret", eta_ttl_seconds=30.0, clock=lambda: fake_now[0])
    asyncio.run(provider.fetch_eta_at_stop("雲林科技大學"))
    first_calls = len(calls)
    fake_now[0] += 15
    asyncio.run(provider.fetch_eta_at_stop("雲林科技大學"))
    assert len(calls) == first_calls  # cache hit, no new requests


def test_fetch_eta_at_stop_refetches_after_ttl(monkeypatch):
    calls = []

    class CountingClient:
        async def post(self, url, **kwargs):
            return _FakeResp(_TOKEN)

        async def get(self, url, **kwargs):
            calls.append(url)
            return _FakeResp(_ETA_ROWS)

    monkeypatch.setattr(tdx_bus, "get_http_client", lambda: CountingClient())
    fake_now = [0.0]
    provider = TdxBusProvider("id", "secret", eta_ttl_seconds=30.0, clock=lambda: fake_now[0])
    asyncio.run(provider.fetch_eta_at_stop("雲林科技大學"))
    first_calls = len(calls)
    fake_now[0] += 31
    asyncio.run(provider.fetch_eta_at_stop("雲林科技大學"))
    assert len(calls) > first_calls  # cache expired, re-fetched


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
    fake_now[0] += 5
    asyncio.run(provider.fetch_route_estimate("201"))
    assert len(calls) == fetch1


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
    fake_now[0] += 11
    asyncio.run(provider.fetch_route_estimate("201"))
    assert len(calls) > fetch1


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
    assert len(post_calls) == 1


def test_fetch_routes_at_stop_deduplicates(monkeypatch):
    _patch_http(monkeypatch, _TOKEN, _STOP_OF_ROUTE)
    provider = TdxBusProvider("id", "secret")
    routes = asyncio.run(provider.fetch_routes_at_stop("雲林科技大學"))
    names = [r["sub_route_name"] for r in routes]
    assert names.count("201") == 1


def test_fetch_route_estimate_city_uses_single_endpoint(monkeypatch):
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
    assert len(rows) == 2


def test_fetch_eta_at_stop_raises_when_both_endpoints_fail(monkeypatch):
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


def test_get_retries_on_429_then_succeeds(monkeypatch):
    """_get retries up to _MAX_RETRIES times on 429 response before succeeding."""
    calls = []
    sleeps = []

    class RetryClient:
        async def post(self, url, **kwargs):
            return _FakeResp(_TOKEN)

        async def get(self, url, **kwargs):
            calls.append(url)
            # First 2 calls return 429; 3rd succeeds
            if len(calls) <= 2:
                return _FakeResp([], status_code=429)
            return _FakeResp(_ETA_ROWS)

    async def fake_sleep(s: float) -> None:
        sleeps.append(s)

    monkeypatch.setattr(tdx_bus, "get_http_client", lambda: RetryClient())
    provider = TdxBusProvider("id", "secret", sleep=fake_sleep)
    provider._kiosk_uids["A"] = {"UID1"}
    rows = asyncio.run(provider.fetch_eta_at_stop("A"))

    # 1 sleep (backoff before attempt 2); data from successful call
    assert len(sleeps) == 1
    assert sleeps == [1.0]  # 1<<0
    assert any(r["sub_route_name"] == "201" for r in rows)


def test_get_raises_after_max_retries(monkeypatch):
    """_get raises after exhausting all retries on persistent 429."""
    import httpx

    class Always429Client:
        async def post(self, url, **kwargs):
            return _FakeResp(_TOKEN)

        async def get(self, url, **kwargs):
            return _FakeResp([], status_code=429)

    async def fake_sleep(s: float) -> None:
        pass

    monkeypatch.setattr(tdx_bus, "get_http_client", lambda: Always429Client())
    provider = TdxBusProvider("id", "secret", sleep=fake_sleep)
    provider._kiosk_uids["A"] = {"UID1"}
    try:
        asyncio.run(provider.fetch_eta_at_stop("A"))
        assert False, "should have raised"
    except httpx.HTTPStatusError as e:
        assert "429" in str(e)


def test_fetch_eta_at_stop_returns_stale_on_error(monkeypatch):
    """When TDX fails but stale cache exists, return stale data instead of raising."""

    class Always429Client:
        async def post(self, url, **kwargs):
            return _FakeResp(_TOKEN)

        async def get(self, url, **kwargs):
            return _FakeResp([], status_code=429)

    async def fake_sleep(s: float) -> None:
        pass

    monkeypatch.setattr(tdx_bus, "get_http_client", lambda: Always429Client())
    fake_now = [0.0]
    provider = TdxBusProvider("id", "secret", eta_ttl_seconds=30.0, sleep=fake_sleep, clock=lambda: fake_now[0])
    provider._kiosk_uids["A"] = {"UID1"}

    # Seed stale cache manually (expired)
    stale_rows = [{"sub_route_name": "201", "direction": 0, "stop_status": 0, "estimate_seconds": 120, "stop_sequence": 1}]
    provider._eta_cache["A"] = (0.0, stale_rows)
    fake_now[0] = 60.0  # TTL expired

    rows = asyncio.run(provider.fetch_eta_at_stop("A"))
    assert rows == stale_rows  # stale returned, not raised
    # Cache timestamp should be refreshed to prevent immediate retry
    assert provider._eta_cache["A"][0] == 60.0
