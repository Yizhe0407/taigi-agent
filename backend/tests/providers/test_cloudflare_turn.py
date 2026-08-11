from __future__ import annotations

import asyncio

import httpx
import pytest

from providers import cloudflare_turn


class _FakeClient:
    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        self.calls: list[dict] = []

    async def post(self, url: str, **kwargs) -> httpx.Response:
        self.calls.append({"url": url, **kwargs})
        return self.response


def _response(payload: dict, status_code: int = 200) -> httpx.Response:
    request = httpx.Request("POST", "https://rtc.live.cloudflare.com/test")
    return httpx.Response(status_code, json=payload, request=request)


def _configure(monkeypatch) -> None:
    monkeypatch.setenv("CLOUDFLARE_TURN_KEY_ID", "key-id")
    monkeypatch.setenv("CLOUDFLARE_TURN_KEY_API_TOKEN", "persistent-api-token")
    monkeypatch.setenv("CLOUDFLARE_TURN_TTL_SECONDS", "3600")
    cloudflare_turn.reset_turn_ice_server_cache()


def test_unconfigured_returns_empty_lan_configuration(monkeypatch):
    monkeypatch.delenv("CLOUDFLARE_TURN_KEY_ID", raising=False)
    monkeypatch.delenv("CLOUDFLARE_TURN_KEY_API_TOKEN", raising=False)
    cloudflare_turn.reset_turn_ice_server_cache()

    result = asyncio.run(cloudflare_turn.get_turn_ice_servers())

    assert result.browser == ()
    assert result.aiortc == ()


def test_fetches_and_caches_short_lived_credentials(monkeypatch):
    _configure(monkeypatch)
    fake = _FakeClient(
        _response(
            {
                "iceServers": [
                    {"urls": ["stun:stun.cloudflare.com:3478"]},
                    {
                        "urls": [
                            "turn:turn.cloudflare.com:3478?transport=udp",
                            "turns:turn.cloudflare.com:5349?transport=tcp",
                        ],
                        "username": "short-lived-user",
                        "credential": "short-lived-password",
                    },
                ]
            }
        )
    )
    monkeypatch.setattr(cloudflare_turn, "get_http_client", lambda: fake)

    first = asyncio.run(cloudflare_turn.get_turn_ice_servers())
    second = asyncio.run(cloudflare_turn.get_turn_ice_servers())

    assert first is second
    assert len(fake.calls) == 1
    assert fake.calls[0]["url"].endswith("/key-id/credentials/generate-ice-servers")
    assert fake.calls[0]["headers"] == {"Authorization": "Bearer persistent-api-token"}
    assert fake.calls[0]["json"] == {"ttl": 3600}
    assert first.browser[1].get("username") == "short-lived-user"
    assert first.browser[1].get("credential") == "short-lived-password"
    assert first.aiortc[1].urls[0].startswith("turn:")


def test_rejects_incomplete_configuration(monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_TURN_KEY_ID", "key-id")
    monkeypatch.delenv("CLOUDFLARE_TURN_KEY_API_TOKEN", raising=False)
    cloudflare_turn.reset_turn_ice_server_cache()

    with pytest.raises(cloudflare_turn.CloudflareTurnConfigurationError):
        asyncio.run(cloudflare_turn.get_turn_ice_servers())


def test_rejects_response_without_turn_credentials(monkeypatch):
    _configure(monkeypatch)
    fake = _FakeClient(_response({"iceServers": [{"urls": "stun:stun.cloudflare.com:3478"}]}))
    monkeypatch.setattr(cloudflare_turn, "get_http_client", lambda: fake)

    with pytest.raises(cloudflare_turn.CloudflareTurnUpstreamError):
        asyncio.run(cloudflare_turn.get_turn_ice_servers())
