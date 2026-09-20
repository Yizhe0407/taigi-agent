from __future__ import annotations

import asyncio

import pytest

from providers.bike import BikeProviderApiError, BikeStation
from providers.fallback_bike import FallbackBikeProvider


def _station(provider: str) -> BikeStation:
    return BikeStation(
        station_uid=f"{provider}-1",
        station_id=None,
        name="測試站",
        latitude=23.7,
        longitude=120.5,
        bike_capacity=None,
        available_rent_bikes=1,
        available_return_bikes=None,
        service_status=None,
        update_time=None,
        provider=provider,
    )


class _Provider:
    def __init__(self, name: str, result=None, error: Exception | None = None) -> None:
        self.name = name
        self.result = result
        self.error = error
        self.calls = 0

    async def fetch_stations(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result


def test_fallback_tries_providers_in_order():
    primary = _Provider("primary", error=BikeProviderApiError("quota"))
    fallback = _Provider("fallback", result=(_station("fallback"),))

    result = asyncio.run(FallbackBikeProvider([primary, fallback]).fetch_stations())

    assert result[0].provider == "fallback"
    assert primary.calls == 1
    assert fallback.calls == 1


def test_fallback_skips_provider_that_returns_no_stations():
    primary = _Provider("primary", result=())
    fallback = _Provider("fallback", result=(_station("fallback"),))

    result = asyncio.run(FallbackBikeProvider([primary, fallback]).fetch_stations())

    assert result[0].provider == "fallback"
    assert primary.calls == 1
    assert fallback.calls == 1


def test_fallback_reports_failure_when_every_provider_is_empty():
    primary = _Provider("primary", result=())
    fallback = _Provider("fallback", result=())

    with pytest.raises(BikeProviderApiError, match="returned no stations"):
        asyncio.run(FallbackBikeProvider([primary, fallback]).fetch_stations())


def test_fallback_reports_all_provider_failures():
    primary = _Provider("primary", error=BikeProviderApiError("quota"))
    fallback = _Provider("fallback", error=RuntimeError("offline"))

    with pytest.raises(BikeProviderApiError, match="all bike providers failed"):
        asyncio.run(FallbackBikeProvider([primary, fallback]).fetch_stations())
