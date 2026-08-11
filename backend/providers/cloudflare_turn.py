"""Short-lived Cloudflare Realtime TURN credentials for WebRTC clients."""

from __future__ import annotations

import asyncio
import hashlib
import os
import time
from dataclasses import dataclass
from typing import Any, TypedDict

import httpx
from aiortc import RTCIceServer

from .http import get_http_client

_API_BASE_URL = "https://rtc.live.cloudflare.com/v1/turn/keys"
_DEFAULT_TTL_SECONDS = 86_400
_MIN_TTL_SECONDS = 60
_MAX_TTL_SECONDS = 86_400


class BrowserIceServer(TypedDict, total=False):
    urls: str | list[str]
    username: str
    credential: str


class CloudflareTurnError(RuntimeError):
    """Base error for TURN credential configuration or retrieval failures."""


class CloudflareTurnConfigurationError(CloudflareTurnError):
    """The TURN environment variables are incomplete or invalid."""


class CloudflareTurnUpstreamError(CloudflareTurnError):
    """Cloudflare did not return usable short-lived TURN credentials."""


@dataclass(frozen=True)
class TurnIceServers:
    browser: tuple[BrowserIceServer, ...]
    aiortc: tuple[RTCIceServer, ...]


_cache_lock = asyncio.Lock()
_cache_key: tuple[str, bytes, int] | None = None
_cache_until = 0.0
_cache_value: TurnIceServers | None = None


def _configuration() -> tuple[str, str, int] | None:
    key_id = os.getenv("CLOUDFLARE_TURN_KEY_ID", "").strip()
    api_token = os.getenv("CLOUDFLARE_TURN_KEY_API_TOKEN", "").strip()

    if not key_id and not api_token:
        return None
    if not key_id or not api_token:
        raise CloudflareTurnConfigurationError("CLOUDFLARE_TURN_KEY_ID and CLOUDFLARE_TURN_KEY_API_TOKEN must be set together")

    raw_ttl = os.getenv("CLOUDFLARE_TURN_TTL_SECONDS", str(_DEFAULT_TTL_SECONDS)).strip()
    try:
        ttl = int(raw_ttl)
    except ValueError as exc:
        raise CloudflareTurnConfigurationError("CLOUDFLARE_TURN_TTL_SECONDS must be an integer") from exc
    if not _MIN_TTL_SECONDS <= ttl <= _MAX_TTL_SECONDS:
        raise CloudflareTurnConfigurationError(f"CLOUDFLARE_TURN_TTL_SECONDS must be between {_MIN_TTL_SECONDS} and {_MAX_TTL_SECONDS}")

    return key_id, api_token, ttl


def _parse_ice_servers(payload: Any) -> TurnIceServers:
    if not isinstance(payload, dict) or not isinstance(payload.get("iceServers"), list):
        raise CloudflareTurnUpstreamError("Cloudflare TURN response did not contain iceServers")

    browser: list[BrowserIceServer] = []
    aiortc_servers: list[RTCIceServer] = []
    has_turn = False

    for raw_server in payload["iceServers"]:
        if not isinstance(raw_server, dict):
            continue

        raw_urls = raw_server.get("urls")
        if isinstance(raw_urls, str):
            urls: str | list[str] = raw_urls
            urls_to_check = [raw_urls]
        elif isinstance(raw_urls, list) and raw_urls and all(isinstance(url, str) for url in raw_urls):
            urls = list(raw_urls)
            urls_to_check = urls
        else:
            continue

        username = raw_server.get("username")
        credential = raw_server.get("credential")
        server: BrowserIceServer = {"urls": urls}
        if isinstance(username, str):
            server["username"] = username
        if isinstance(credential, str):
            server["credential"] = credential

        if any(url.startswith(("turn:", "turns:")) for url in urls_to_check):
            if not isinstance(username, str) or not username or not isinstance(credential, str) or not credential:
                continue
            has_turn = True

        browser.append(server)
        aiortc_servers.append(RTCIceServer(urls=urls, username=username, credential=credential))

    if not browser or not has_turn:
        raise CloudflareTurnUpstreamError("Cloudflare TURN response did not contain a usable TURN server")

    return TurnIceServers(browser=tuple(browser), aiortc=tuple(aiortc_servers))


async def get_turn_ice_servers() -> TurnIceServers:
    """Return cached short-lived ICE servers, or an empty set for LAN-only mode."""

    global _cache_key, _cache_until, _cache_value

    configured = _configuration()
    if configured is None:
        return TurnIceServers(browser=(), aiortc=())

    key_id, api_token, ttl = configured
    token_fingerprint = hashlib.sha256(api_token.encode()).digest()
    current_key = (key_id, token_fingerprint, ttl)
    now = time.monotonic()
    if _cache_key == current_key and _cache_value is not None and now < _cache_until:
        return _cache_value

    async with _cache_lock:
        now = time.monotonic()
        if _cache_key == current_key and _cache_value is not None and now < _cache_until:
            return _cache_value

        try:
            response = await get_http_client().post(
                f"{_API_BASE_URL}/{key_id}/credentials/generate-ice-servers",
                headers={"Authorization": f"Bearer {api_token}"},
                json={"ttl": ttl},
            )
            response.raise_for_status()
            value = _parse_ice_servers(response.json())
        except CloudflareTurnError:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            raise CloudflareTurnUpstreamError("Failed to obtain Cloudflare TURN credentials") from exc

        # Refresh before expiry. For the default 24-hour credential this keeps a
        # five-minute safety margin; short test/dev TTLs refresh after 80%.
        refresh_margin = min(300.0, ttl * 0.2)
        _cache_key = current_key
        _cache_until = time.monotonic() + ttl - refresh_margin
        _cache_value = value
        return value


def reset_turn_ice_server_cache() -> None:
    """Clear process cache (used by tests and explicit runtime reconfiguration)."""

    global _cache_key, _cache_until, _cache_value
    _cache_key = None
    _cache_until = 0.0
    _cache_value = None
