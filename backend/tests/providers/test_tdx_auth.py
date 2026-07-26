"""Tests for the shared TdxTokenClient (providers/tdx_auth.py).

Covers token fetch/cache, concurrent-miss dedupe, and the 401 → forced
refresh → retry-once behavior shared by TdxBusProvider and TdxBikeProvider.
"""

from __future__ import annotations

import asyncio

from providers.tdx_auth import TdxTokenClient


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


def test_get_token_is_cached_across_calls():
    post_calls = []

    class CountingClient:
        async def post(self, url, **kwargs):
            post_calls.append(url)
            return _FakeResp(_TOKEN)

    client = TdxTokenClient("id", "secret", http_client_factory=lambda: CountingClient())

    token1 = asyncio.run(client.get_token())
    token2 = asyncio.run(client.get_token())

    assert token1 == token2 == "fake-token"
    assert len(post_calls) == 1


def test_get_token_concurrent_miss_calls_upstream_once():
    post_calls = []

    class SlowClient:
        async def post(self, url, **kwargs):
            post_calls.append(url)
            await asyncio.sleep(0.05)  # force both callers to be in-flight together
            return _FakeResp(_TOKEN)

    client = TdxTokenClient("id", "secret", http_client_factory=lambda: SlowClient())

    async def run():
        await asyncio.gather(client.get_token(), client.get_token())

    asyncio.run(run())
    assert len(post_calls) == 1


def test_request_with_retry_refreshes_token_once_on_401():
    """A 401 forces a fresh token and retries the request exactly once."""
    post_calls = []
    tokens_used = []

    class FakeClient:
        async def post(self, url, **kwargs):
            post_calls.append(url)
            return _FakeResp({"access_token": f"token-{len(post_calls)}", "expires_in": 3600})

    client = TdxTokenClient("id", "secret", http_client_factory=lambda: FakeClient())

    async def do_request(token: str) -> _FakeResp:
        tokens_used.append(token)
        if len(tokens_used) == 1:
            return _FakeResp({}, status_code=401)
        return _FakeResp({"ok": True})

    resp = asyncio.run(client.request_with_retry(do_request))

    assert resp.status_code == 200
    assert tokens_used == ["token-1", "token-2"]  # second attempt used a refreshed token
    assert len(post_calls) == 2  # initial fetch + forced refresh after the 401


def test_request_with_retry_does_not_retry_a_second_401():
    """Only one retry is attempted; a persistent 401 is returned as-is."""
    post_calls = []
    attempts = []

    class FakeClient:
        async def post(self, url, **kwargs):
            post_calls.append(url)
            return _FakeResp(_TOKEN)

    client = TdxTokenClient("id", "secret", http_client_factory=lambda: FakeClient())

    async def always_401(token: str) -> _FakeResp:
        attempts.append(token)
        return _FakeResp({}, status_code=401)

    resp = asyncio.run(client.request_with_retry(always_401))

    assert resp.status_code == 401
    assert len(attempts) == 2  # original attempt + exactly one retry
    assert len(post_calls) == 2  # initial fetch + the one forced refresh
