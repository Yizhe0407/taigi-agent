"""Tests for TdxBikeProvider OAuth token caching (providers/moovo.py).

Domain parsing/caching lives in services/moovo.py (see tests/services/test_moovo.py);
this file only covers the TDX token fetch/cache mechanics.
"""

from __future__ import annotations

import asyncio

from providers.moovo import TdxBikeProvider


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


_TOKEN = {"access_token": "fake-token", "expires_in": 3600}


def test_get_token_concurrent_miss_calls_upstream_once(monkeypatch):
    """Two concurrent token cache misses must only POST to the token endpoint once."""
    monkeypatch.setenv("TDX_CLIENT_ID", "id")
    monkeypatch.setenv("TDX_CLIENT_SECRET", "secret")
    calls = []

    class SlowClient:
        async def post(self, url, **kwargs):
            calls.append(url)
            await asyncio.sleep(0.05)  # force both callers to be in-flight together
            return _FakeResp(_TOKEN)

    client = SlowClient()
    provider = TdxBikeProvider()

    async def run():
        await asyncio.gather(
            provider._get_token(client),
            provider._get_token(client),
        )

    asyncio.run(run())
    assert len(calls) == 1


def test_get_token_is_cached_across_calls(monkeypatch):
    monkeypatch.setenv("TDX_CLIENT_ID", "id")
    monkeypatch.setenv("TDX_CLIENT_SECRET", "secret")
    calls = []

    class CountingClient:
        async def post(self, url, **kwargs):
            calls.append(url)
            return _FakeResp(_TOKEN)

    client = CountingClient()
    provider = TdxBikeProvider()

    token1 = asyncio.run(provider._get_token(client))
    token2 = asyncio.run(provider._get_token(client))

    assert token1 == token2 == "fake-token"
    assert len(calls) == 1
