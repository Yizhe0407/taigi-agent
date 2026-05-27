"""MOOVO bike station endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import NoReturn

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from services.moovo import (
    MoovoApiError,
    MoovoConfigError,
    MoovoError,
    MoovoStation,
    NearbyMoovoStation,
    load_moovo_stations,
    nearby_moovo_stations,
)

router = APIRouter()


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class MoovoStationResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    station_uid: str = Field(alias="stationUid")
    station_id: str | None = Field(alias="stationId")
    name: str
    lat: float
    lng: float
    bike_capacity: int = Field(alias="bikeCapacity")
    available_rent_bikes: int = Field(alias="availableRentBikes")
    available_return_bikes: int = Field(alias="availableReturnBikes")
    service_status: int = Field(alias="serviceStatus")
    update_time: datetime | None = Field(alias="updateTime")


class NearbyMoovoStationResponse(MoovoStationResponse):
    distance_meters: float = Field(alias="distanceMeters")


class MoovoStationsResponse(BaseModel):
    stations: list[MoovoStationResponse]


class NearbyMoovoStationsResponse(BaseModel):
    stations: list[NearbyMoovoStationResponse]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _moovo_station_response(station: MoovoStation) -> dict[str, object]:
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
    }


def _nearby_moovo_station_response(item: NearbyMoovoStation) -> dict[str, object]:
    payload = _moovo_station_response(item.station)
    payload["distanceMeters"] = item.distance_meters
    return payload


def _raise_moovo_unavailable(error: MoovoError) -> NoReturn:
    raise HTTPException(status_code=503, detail=str(error)) from error


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/api/moovo/stations", response_model=MoovoStationsResponse)
async def list_moovo_stations() -> object:
    """Return Yunlin MOOVO stations with current TDX availability."""
    try:
        stations = await load_moovo_stations()
    except (MoovoApiError, MoovoConfigError) as error:
        _raise_moovo_unavailable(error)
    return {"stations": [_moovo_station_response(station) for station in stations]}


@router.get("/api/moovo/stations/nearby", response_model=NearbyMoovoStationsResponse)
async def list_nearby_moovo_stations(
    lat: float = Query(ge=-90, le=90),
    lng: float = Query(ge=-180, le=180),
    radius: int = Query(default=1000, ge=1, le=5000),
    limit: int = Query(default=20, ge=1, le=20),
) -> object:
    """Return Yunlin MOOVO stations near a frontend-selected coordinate."""
    try:
        stations = await nearby_moovo_stations(
            lat,
            lng,
            radius_meters=radius,
            limit=limit,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail="目的地座標格式有誤") from error
    except (MoovoApiError, MoovoConfigError) as error:
        _raise_moovo_unavailable(error)
    return {"stations": [_nearby_moovo_station_response(item) for item in stations]}
