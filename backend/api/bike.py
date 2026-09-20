"""Public bike-share station endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import NoReturn

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from services.bike import (
    BikeProviderApiError,
    BikeProviderConfigError,
    BikeProviderError,
    BikeStation,
    NearbyBikeStation,
    load_bike_stations,
    nearby_bike_stations,
)

from .request_limits import BIKE_RATE_LIMIT

router = APIRouter()


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class BikeStationResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    station_uid: str = Field(alias="stationUid")
    station_id: str | None = Field(alias="stationId")
    name: str
    lat: float
    lng: float
    bike_capacity: int | None = Field(alias="bikeCapacity")
    available_rent_bikes: int | None = Field(alias="availableRentBikes")
    available_return_bikes: int | None = Field(alias="availableReturnBikes")
    service_status: int | None = Field(alias="serviceStatus")
    update_time: datetime | None = Field(alias="updateTime")
    provider: str = Field(default="unknown")


class NearbyBikeStationResponse(BikeStationResponse):
    distance_meters: float = Field(alias="distanceMeters")


class BikeStationsResponse(BaseModel):
    stations: list[BikeStationResponse]


class NearbyBikeStationsResponse(BaseModel):
    stations: list[NearbyBikeStationResponse]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bike_station_response(station: BikeStation) -> dict[str, object]:
    return {
        "stationUid": station.station_uid,
        "stationId": station.station_id,
        "name": station.name,
        "lat": station.latitude,
        "lng": station.longitude,
        "bikeCapacity": station.bike_capacity,
        "availableRentBikes": station.available_rent_bikes,
        "availableReturnBikes": station.available_return_bikes,
        "serviceStatus": station.service_status,
        "updateTime": station.update_time,
        "provider": station.provider,
    }


def _nearby_bike_station_response(item: NearbyBikeStation) -> dict[str, object]:
    payload = _bike_station_response(item.station)
    payload["distanceMeters"] = item.distance_meters
    return payload


def _raise_bike_unavailable(error: BikeProviderError) -> NoReturn:
    raise HTTPException(status_code=503, detail=str(error)) from error


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/api/bike/stations",
    response_model=BikeStationsResponse,
    dependencies=[Depends(BIKE_RATE_LIMIT)],
)
async def list_bike_stations() -> object:
    """Return normalized public-bike stations from the configured provider chain."""
    try:
        stations = await load_bike_stations()
    except (BikeProviderApiError, BikeProviderConfigError) as error:
        _raise_bike_unavailable(error)
    return {"stations": [_bike_station_response(station) for station in stations]}


@router.get(
    "/api/bike/stations/nearby",
    response_model=NearbyBikeStationsResponse,
    dependencies=[Depends(BIKE_RATE_LIMIT)],
)
async def list_nearby_bike_stations(
    lat: float = Query(ge=-90, le=90),
    lng: float = Query(ge=-180, le=180),
    radius: int = Query(default=1000, ge=1, le=5000),
    limit: int = Query(default=20, ge=1, le=20),
) -> object:
    """Return Yunlin public-bike stations near a frontend-selected coordinate."""
    try:
        stations = await nearby_bike_stations(
            lat,
            lng,
            radius_meters=radius,
            limit=limit,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail="目的地座標格式有誤") from error
    except (BikeProviderApiError, BikeProviderConfigError) as error:
        _raise_bike_unavailable(error)
    return {"stations": [_nearby_bike_station_response(item) for item in stations]}
