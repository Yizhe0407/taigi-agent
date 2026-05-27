"""Route planning and kiosk info endpoints."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from config import Settings
from services.route_plans import (
    InvalidRouteDestination,
    RoutePlanningUnavailable,
    RoutePlanNotFound,
    plan_route_to_coordinate,
    route_plan_to_view_model,
)

router = APIRouter()

LngLat = tuple[float, float]


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class DestinationRequest(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)


class RoutePlanRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    destination: DestinationRequest
    departure_time: datetime | None = Field(default=None, alias="departureTime")

    @field_validator("departure_time")
    @classmethod
    def require_departure_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("departureTime must include a timezone")
        return value


class PlaceResponse(BaseModel):
    name: str
    lat: float
    lng: float


class RouteNameResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    short_name: str | None = Field(alias="shortName")
    long_name: str | None = Field(alias="longName")


class LegResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    mode: str
    from_name: str = Field(alias="fromName")
    to_name: str = Field(alias="toName")
    start: datetime
    end: datetime
    duration: float
    distance: float
    coordinates: list[LngLat]
    route: RouteNameResponse | None


class RouteOptionResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str
    coordinates: list[LngLat]
    duration: int
    distance: float
    transfer_count: int = Field(alias="transferCount")
    legs: list[LegResponse]


class RoutePlanResponse(BaseModel):
    origin: PlaceResponse
    destination: PlaceResponse
    routes: list[RouteOptionResponse]


class KioskResponse(BaseModel):
    name: str
    lat: float
    lng: float


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/api/kiosk", response_model=KioskResponse)
def get_kiosk() -> object:
    """Return the kiosk stop name and its actual OTP origin coordinates."""
    from services.route_plans import _kiosk_place  # noqa: PLC0415

    place = _kiosk_place()
    if place is None:
        stop = Settings.from_env().kiosk_stop
        raise HTTPException(
            status_code=503, detail=f"找不到站牌「{stop}」的座標資料"
        )
    return KioskResponse(
        name=place.name,
        lat=place.coordinate.latitude,
        lng=place.coordinate.longitude,
    )


@router.post("/api/route-plans", response_model=RoutePlanResponse)
async def create_route_plan(request: RoutePlanRequest) -> object:
    """Plan from the configured Kiosk origin to a frontend-selected destination."""
    try:
        plan = await plan_route_to_coordinate(
            request.destination.lat,
            request.destination.lng,
            request.departure_time,
        )
    except InvalidRouteDestination as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except RoutePlanNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except RoutePlanningUnavailable as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    return route_plan_to_view_model(plan)
