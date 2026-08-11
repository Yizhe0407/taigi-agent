from __future__ import annotations

from aiortc import RTCIceServer
from fastapi.testclient import TestClient

import api
from api import voice
from providers.cloudflare_turn import BrowserIceServer, TurnIceServers


def test_voice_ice_servers_returns_browser_credentials_and_updates_handler(monkeypatch):
    browser_server: BrowserIceServer = {
        "urls": ["turn:turn.cloudflare.com:3478?transport=udp"],
        "username": "temporary-user",
        "credential": "temporary-password",
    }
    server_side = RTCIceServer(
        urls=browser_server["urls"],
        username=browser_server["username"],
        credential=browser_server["credential"],
    )

    async def fake_servers() -> TurnIceServers:
        return TurnIceServers(browser=(browser_server,), aiortc=(server_side,))

    updated = []
    monkeypatch.setattr(voice, "get_turn_ice_servers", fake_servers)
    monkeypatch.setattr(voice._handler, "update_ice_servers", updated.append)

    response = TestClient(api.app).get("/api/voice/ice-servers")

    assert response.status_code == 200
    assert response.json() == {"iceServers": [browser_server]}
    assert updated == [[server_side]]


def test_voice_ice_servers_maps_upstream_failure(monkeypatch):
    async def unavailable() -> TurnIceServers:
        raise voice.CloudflareTurnUpstreamError("upstream unavailable")

    monkeypatch.setattr(voice, "get_turn_ice_servers", unavailable)

    response = TestClient(api.app).get("/api/voice/ice-servers")

    assert response.status_code == 502
    assert response.json() == {"detail": "WebRTC TURN is temporarily unavailable"}
