"""Tests for TdxBikeProvider (providers/moovo.py).

Token fetch/cache/401-retry mechanics themselves live in TdxTokenClient (see
tests/providers/test_tdx_auth.py); this file covers TdxBikeProvider's own
wiring: lazy credential resolution, concurrent cold-start dedupe, and that a
GET 401 is transparently retried once via the shared token client.

Domain parsing/caching lives in services/moovo.py (see tests/services/test_moovo.py).
"""

from __future__ import annotations

import asyncio

import pytest

from providers import moovo
from providers.moovo import MoovoConfigError, TdxBikeProvider

_TOKEN = {"access_token": "fake-token", "expires_in": 3600}


class _FakeResp:
    def __init__(self, payload, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx

            raise httpx.HTTPStatusError(str(self.status_code), request=None, response=self)

    def json(self):
        return self._payload


def test_missing_credentials_raises_config_error(monkeypatch):
    monkeypatch.delenv("TDX_CLIENT_ID", raising=False)
    monkeypatch.delenv("TDX_CLIENT_SECRET", raising=False)
    provider = TdxBikeProvider()
    with pytest.raises(MoovoConfigError):
        asyncio.run(provider.fetch_station_payloads())


def test_get_token_is_cached_across_calls(monkeypatch):
    monkeypatch.setenv("TDX_CLIENT_ID", "id")
    monkeypatch.setenv("TDX_CLIENT_SECRET", "secret")
    post_calls = []

    class FakeClient:
        async def post(self, url, **kwargs):
            post_calls.append(url)
            return _FakeResp(_TOKEN)

        async def get(self, url, **kwargs):
            return _FakeResp([])

    monkeypatch.setattr(moovo, "get_http_client", lambda: FakeClient())
    provider = TdxBikeProvider()

    asyncio.run(provider.fetch_station_payloads())
    asyncio.run(provider.fetch_station_payloads())

    assert len(post_calls) == 1


def test_get_token_concurrent_miss_calls_upstream_once(monkeypatch):
    """Concurrent cold-start requests (each gathering 2 GETs) must only POST once."""
    monkeypatch.setenv("TDX_CLIENT_ID", "id")
    monkeypatch.setenv("TDX_CLIENT_SECRET", "secret")
    post_calls = []

    class SlowClient:
        async def post(self, url, **kwargs):
            post_calls.append(url)
            await asyncio.sleep(0.05)  # force both callers to be in-flight together
            return _FakeResp(_TOKEN)

        async def get(self, url, **kwargs):
            return _FakeResp([])

    monkeypatch.setattr(moovo, "get_http_client", lambda: SlowClient())
    provider = TdxBikeProvider()

    async def run():
        await asyncio.gather(
            provider.fetch_station_payloads(),
            provider.fetch_station_payloads(),
        )

    asyncio.run(run())
    assert len(post_calls) == 1


def test_401_on_get_forces_token_refresh_and_retries_once(monkeypatch):
    """A 401 on the Station GET is transparently retried once with a fresh token."""
    monkeypatch.setenv("TDX_CLIENT_ID", "id")
    monkeypatch.setenv("TDX_CLIENT_SECRET", "secret")
    post_calls = []
    station_attempts: list[str] = []

    class FakeClient:
        async def post(self, url, **kwargs):
            post_calls.append(url)
            return _FakeResp({"access_token": f"token-{len(post_calls)}", "expires_in": 3600})

        async def get(self, url, **kwargs):
            if "/Station/City/" in url:
                station_attempts.append(kwargs["headers"]["Authorization"])
                if len(station_attempts) == 1:
                    return _FakeResp({}, status_code=401)
            return _FakeResp([])

    monkeypatch.setattr(moovo, "get_http_client", lambda: FakeClient())
    provider = TdxBikeProvider()

    stations, availability = asyncio.run(provider.fetch_station_payloads())

    assert stations == []
    assert availability == []
    assert len(station_attempts) == 2
    assert station_attempts[0] != station_attempts[1]  # second attempt used a refreshed token
    assert len(post_calls) == 2  # initial token fetch + the forced refresh after the 401


def test_reset_token_cache_forces_refetch(monkeypatch):
    monkeypatch.setenv("TDX_CLIENT_ID", "id")
    monkeypatch.setenv("TDX_CLIENT_SECRET", "secret")
    post_calls = []

    class FakeClient:
        async def post(self, url, **kwargs):
            post_calls.append(url)
            return _FakeResp(_TOKEN)

        async def get(self, url, **kwargs):
            return _FakeResp([])

    monkeypatch.setattr(moovo, "get_http_client", lambda: FakeClient())
    provider = TdxBikeProvider()

    asyncio.run(provider.fetch_station_payloads())
    provider.reset_token_cache()
    asyncio.run(provider.fetch_station_payloads())

    assert len(post_calls) == 2
