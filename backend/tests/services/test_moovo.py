from __future__ import annotations

import asyncio
from datetime import datetime

from providers.moovo import TdxBikeProvider
from services import moovo


def _station_payload(
    station_uid: str,
    name: str,
    *,
    latitude: float = 23.696147,
    longitude: float = 120.534823,
) -> dict[str, object]:
    return {
        "StationUID": station_uid,
        "StationID": station_uid.removeprefix("YUN"),
        "StationName": {"Zh_tw": name},
        "BikesCapacity": 18,
        "StationPosition": {
            "PositionLat": latitude,
            "PositionLon": longitude,
        },
    }


class FakeTdxBikeProvider(TdxBikeProvider):
    """Skip TDX HTTP; deliver injected payloads.

    Inherits the parent class so type checks against TdxBikeProvider pass —
    overriding only the network entry point keeps the contract honest.
    """

    def __init__(
        self,
        stations: list[object],
        availability: list[object],
    ) -> None:
        super().__init__()
        self._payloads = (stations, availability)
        self.fetch_count = 0

    async def fetch_station_payloads(self):  # type: ignore[override]
        self.fetch_count += 1
        return self._payloads


def test_merge_station_payloads_combines_tdx_station_and_availability() -> None:
    stations = [
        _station_payload("YUN100", "雲林科技大學"),
        _station_payload("YUN101", "雲林科技大學(龍潭路)", latitude=23.693017),
        {"StationUID": "BROKEN"},
    ]
    availability = [
        {
            "StationUID": "YUN100",
            "AvailableRentBikes": 6,
            "AvailableReturnBikes": 4,
            "ServiceStatus": 1,
            "UpdateTime": "2026-05-22T08:30:00+08:00",
        },
        {
            "StationUID": "YUN101",
            "AvailableRentBikes": 0,
            "AvailableReturnBikes": 9,
            "ServiceStatus": 2,
        },
    ]

    merged = moovo._merge_station_payloads(stations, availability)

    assert [station.station_uid for station in merged] == ["YUN100", "YUN101"]
    assert merged[0].name == "雲林科技大學"
    assert merged[0].station_id == "100"
    assert merged[0].latitude == 23.696147
    assert merged[0].longitude == 120.534823
    assert merged[0].bike_capacity == 18
    assert merged[0].available_rent_bikes == 6
    assert merged[0].available_return_bikes == 4
    assert merged[0].service_status == 1
    assert merged[0].update_time == datetime.fromisoformat(
        "2026-05-22T08:30:00+08:00"
    )
    assert merged[1].available_rent_bikes == 0
    assert merged[1].service_status == 2


def test_nearby_moovo_stations_filters_and_sorts(monkeypatch) -> None:
    stations = (
        moovo.MoovoStation(
            station_uid="YUN100",
            station_id="100",
            name="遠站",
            latitude=23.8,
            longitude=120.7,
            bike_capacity=10,
            available_rent_bikes=1,
            available_return_bikes=1,
            service_status=1,
            update_time=None,
        ),
        moovo.MoovoStation(
            station_uid="YUN101",
            station_id="101",
            name="近站",
            latitude=23.6962,
            longitude=120.5349,
            bike_capacity=12,
            available_rent_bikes=3,
            available_return_bikes=4,
            service_status=1,
            update_time=None,
        ),
    )
    async def fake_load_stations():
        return stations

    monkeypatch.setattr(moovo, "load_moovo_stations", fake_load_stations)

    nearby = asyncio.run(
        moovo.nearby_moovo_stations(
            23.696147,
            120.534823,
            radius_meters=100,
        )
    )

    assert len(nearby) == 1
    assert nearby[0].station.station_uid == "YUN101"
    assert nearby[0].distance_meters < 20


def test_load_moovo_stations_uses_cache(monkeypatch) -> None:
    provider = FakeTdxBikeProvider(
        stations=[_station_payload("YUN100", "雲林科技大學")],
        availability=[{"StationUID": "YUN100", "AvailableRentBikes": 2}],
    )
    monkeypatch.setenv("MOOVO_CACHE_TTL_SECONDS", "60")

    with moovo.provider_override(provider):
        first = asyncio.run(moovo.load_moovo_stations())
        second = asyncio.run(moovo.load_moovo_stations())

    assert provider.fetch_count == 1
    assert first is second
    assert first[0].available_rent_bikes == 2
