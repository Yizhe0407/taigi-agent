from __future__ import annotations

import asyncio
from datetime import datetime

from providers.bike import BikeStation
from services import bike


def _station(
    station_uid: str,
    name: str,
    *,
    latitude: float = 23.696147,
    longitude: float = 120.534823,
    available_rent_bikes: int | None = 2,
) -> BikeStation:
    return BikeStation(
        station_uid=station_uid,
        station_id=station_uid.removeprefix("YUN"),
        name=name,
        latitude=latitude,
        longitude=longitude,
        bike_capacity=18,
        available_rent_bikes=available_rent_bikes,
        available_return_bikes=4,
        service_status=1,
        update_time=None,
        provider="fake",
    )


class FakeBikeProvider:
    name = "fake"

    def __init__(self, stations: tuple[BikeStation, ...]) -> None:
        self.stations = stations
        self.fetch_count = 0

    async def fetch_stations(self) -> tuple[BikeStation, ...]:
        self.fetch_count += 1
        return self.stations


def test_provider_returns_normalized_station_snapshot() -> None:
    station = _station("YUN100", "雲林科技大學", available_rent_bikes=6)
    provider = FakeBikeProvider((station,))

    with bike.provider_override(provider):
        loaded = asyncio.run(bike.load_bike_stations(force_refresh=True))

    assert loaded == (station,)


def test_nearby_bike_stations_filters_and_sorts(monkeypatch) -> None:
    stations = (
        _station("YUN100", "遠站", latitude=23.8, longitude=120.7, available_rent_bikes=1),
        _station("YUN101", "近站", latitude=23.6962, longitude=120.5349, available_rent_bikes=3),
    )

    async def fake_load_stations():
        return stations

    monkeypatch.setattr(bike, "load_bike_stations", fake_load_stations)

    nearby = asyncio.run(
        bike.nearby_bike_stations(
            23.696147,
            120.534823,
            radius_meters=100,
        )
    )

    assert len(nearby) == 1
    assert nearby[0].station.station_uid == "YUN101"
    assert nearby[0].distance_meters < 20


def test_load_bike_stations_uses_cache(monkeypatch) -> None:
    station = _station("YUN100", "雲林科技大學", available_rent_bikes=2)
    provider = FakeBikeProvider((station,))
    monkeypatch.setenv("BIKE_CACHE_TTL_SECONDS", "60")

    with bike.provider_override(provider):
        first = asyncio.run(bike.load_bike_stations())
        second = asyncio.run(bike.load_bike_stations())

    assert provider.fetch_count == 1
    assert first is second
    assert first[0].available_rent_bikes == 2


def test_provider_switch_clears_cache() -> None:
    first_provider = FakeBikeProvider((_station("YUN100", "第一來源"),))
    second_provider = FakeBikeProvider((_station("YUN200", "第二來源"),))

    with bike.provider_override(first_provider):
        first = asyncio.run(bike.load_bike_stations(force_refresh=True))
        bike.set_provider(second_provider)
        second = asyncio.run(bike.load_bike_stations())

    assert first[0].name == "第一來源"
    assert second[0].name == "第二來源"
    assert first_provider.fetch_count == 1
    assert second_provider.fetch_count == 1


def test_unknown_provider_fields_stay_unknown() -> None:
    station = BikeStation(
        station_uid="web-1",
        station_id=None,
        name="官網站點",
        latitude=23.7,
        longitude=120.5,
        bike_capacity=None,
        available_rent_bikes=3,
        available_return_bikes=None,
        service_status=None,
        update_time=datetime.fromisoformat("2026-09-20T03:00:00+00:00"),
        provider="moovo_web",
    )
    provider = FakeBikeProvider((station,))

    with bike.provider_override(provider):
        loaded = asyncio.run(bike.load_bike_stations(force_refresh=True))

    assert loaded[0].bike_capacity is None
    assert loaded[0].available_return_bikes is None
    assert loaded[0].service_status is None
