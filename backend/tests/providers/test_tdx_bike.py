"""Tests for the provider-specific TDX Bike adapter."""

from __future__ import annotations

import asyncio

import pytest

from providers import tdx_bike
from providers.bike import BikeProviderConfigError
from providers.tdx_bike import TdxBikeProvider

_TOKEN = {"access_token": "fake-token", "expires_in": 3600}


class _FakeResp:
    def __init__(self, payload, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code
        self.headers = {}

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx

            raise httpx.HTTPStatusError(str(self.status_code), request=None, response=self)

    def json(self):
        return self._payload

    @property
    def text(self):
        return str(self._payload)


def test_missing_credentials_raises_config_error(monkeypatch):
    monkeypatch.delenv("TDX_CLIENT_ID", raising=False)
    monkeypatch.delenv("TDX_CLIENT_SECRET", raising=False)
    provider = TdxBikeProvider()
    with pytest.raises(BikeProviderConfigError):
        asyncio.run(provider.fetch_stations())


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

    monkeypatch.setattr(tdx_bike, "get_http_client", lambda: FakeClient())
    provider = TdxBikeProvider()

    asyncio.run(provider.fetch_stations())
    asyncio.run(provider.fetch_stations())

    assert len(post_calls) == 1


def test_get_token_concurrent_miss_calls_upstream_once(monkeypatch):
    monkeypatch.setenv("TDX_CLIENT_ID", "id")
    monkeypatch.setenv("TDX_CLIENT_SECRET", "secret")
    post_calls = []

    class SlowClient:
        async def post(self, url, **kwargs):
            post_calls.append(url)
            await asyncio.sleep(0.05)
            return _FakeResp(_TOKEN)

        async def get(self, url, **kwargs):
            return _FakeResp([])

    monkeypatch.setattr(tdx_bike, "get_http_client", lambda: SlowClient())
    provider = TdxBikeProvider()

    async def run():
        await asyncio.gather(provider.fetch_stations(), provider.fetch_stations())

    asyncio.run(run())
    assert len(post_calls) == 1


def test_401_on_get_forces_token_refresh_and_retries_once(monkeypatch):
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

    monkeypatch.setattr(tdx_bike, "get_http_client", lambda: FakeClient())
    provider = TdxBikeProvider()

    stations = asyncio.run(provider.fetch_stations())

    assert stations == ()
    assert len(station_attempts) == 2
    assert station_attempts[0] != station_attempts[1]
    assert len(post_calls) == 2


def test_fetch_stations_normalizes_tdx_payloads(monkeypatch):
    monkeypatch.setenv("TDX_CLIENT_ID", "id")
    monkeypatch.setenv("TDX_CLIENT_SECRET", "secret")

    class FakeClient:
        async def post(self, url, **kwargs):
            return _FakeResp(_TOKEN)

        async def get(self, url, **kwargs):
            if "/Station/City/" in url:
                return _FakeResp(
                    [
                        {
                            "StationUID": "YUN100",
                            "StationID": "100",
                            "StationName": {"Zh_tw": "雲林科技大學"},
                            "BikesCapacity": 18,
                            "StationPosition": {"PositionLat": 23.696147, "PositionLon": 120.534823},
                        }
                    ]
                )
            return _FakeResp(
                [
                    {
                        "StationUID": "YUN100",
                        "AvailableRentBikes": 6,
                        "AvailableReturnBikes": 4,
                        "ServiceStatus": 1,
                        "UpdateTime": "2026-09-20T03:00:00+00:00",
                    }
                ]
            )

    provider = TdxBikeProvider()
    provider._token_client = None
    monkeypatch.setattr(tdx_bike, "get_http_client", lambda: FakeClient())

    stations = asyncio.run(provider.fetch_stations())

    assert len(stations) == 1
    assert stations[0].provider == "tdx"
    assert stations[0].available_rent_bikes == 6
    assert stations[0].available_return_bikes == 4
    assert stations[0].bike_capacity == 18


def test_station_without_availability_row_reports_unknown_counts(monkeypatch):
    monkeypatch.setenv("TDX_CLIENT_ID", "id")
    monkeypatch.setenv("TDX_CLIENT_SECRET", "secret")

    class FakeClient:
        async def post(self, url, **kwargs):
            return _FakeResp(_TOKEN)

        async def get(self, url, **kwargs):
            if "/Station/City/" in url:
                return _FakeResp(
                    [
                        {
                            "StationUID": "YUN101",
                            "StationName": {"Zh_tw": "斗六車站"},
                            "StationPosition": {"PositionLat": 23.711, "PositionLon": 120.541},
                        }
                    ]
                )
            return _FakeResp([])

    provider = TdxBikeProvider()
    monkeypatch.setattr(tdx_bike, "get_http_client", lambda: FakeClient())

    stations = asyncio.run(provider.fetch_stations())

    assert len(stations) == 1
    assert stations[0].available_rent_bikes is None
    assert stations[0].available_return_bikes is None
    assert stations[0].bike_capacity is None
    assert stations[0].service_status is None


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

    monkeypatch.setattr(tdx_bike, "get_http_client", lambda: FakeClient())
    provider = TdxBikeProvider()

    asyncio.run(provider.fetch_stations())
    provider.reset_token_cache()
    asyncio.run(provider.fetch_stations())

    assert len(post_calls) == 2
