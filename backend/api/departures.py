"""Structured departure decision endpoints."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from tools.kiosk_departures import (
    DepartureDecision,
    DepartureRouteDetail,
    DepartureSection,
    DepartureSnapshotUnavailable,
    RouteDetailNotFound,
    RouteDetailUnavailable,
    StopDepartureSnapshot,
    get_departure_snapshot_here,
    get_route_detail_here,
)

router = APIRouter()


class DepartureResponseModel(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


class DepartureSummaryResponse(DepartureResponseModel):

    available_count: int = Field(alias="availableCount")
    not_departed_count: int = Field(alias="notDepartedCount")
    last_departed_count: int = Field(alias="lastDepartedCount")
    unknown_count: int = Field(alias="unknownCount")


class DepartureRouteStatusResponse(DepartureResponseModel):

    id: str
    route: str
    route_id: int = Field(alias="routeId")
    direction: str
    go_back: int = Field(alias="goBack")
    section: DepartureSection
    decision: DepartureDecision
    status_text: str = Field(alias="statusText")
    decision_text: str = Field(alias="decisionText")
    minutes: int | None
    scheduled_time: str | None = Field(alias="scheduledTime")


class StopDepartureSnapshotResponse(DepartureResponseModel):

    stop_name: str = Field(alias="stopName")
    direction_filter: int | None = Field(alias="directionFilter")
    updated_at: datetime = Field(alias="updatedAt")
    summary: DepartureSummaryResponse
    routes: list[DepartureRouteStatusResponse]


class RouteStopDetailResponse(DepartureResponseModel):
    seq: int
    name: str
    is_current_stop: bool = Field(alias="isCurrentStop")
    status_text: str = Field(alias="statusText")
    minutes: int | None
    scheduled_time: str | None = Field(alias="scheduledTime")


class RouteDirectionDetailResponse(DepartureResponseModel):
    go_back: int = Field(alias="goBack")
    label: str
    stops: list[RouteStopDetailResponse]


class DepartureRouteDetailResponse(DepartureResponseModel):
    route: str
    route_id: int = Field(alias="routeId")
    stop_name: str = Field(alias="stopName")
    direction_filter: int | None = Field(alias="directionFilter")
    directions: list[RouteDirectionDetailResponse]


def _snapshot_to_response(
    snapshot: StopDepartureSnapshot,
) -> StopDepartureSnapshotResponse:
    return StopDepartureSnapshotResponse.model_validate(snapshot)


def _route_detail_to_response(
    detail: DepartureRouteDetail,
) -> DepartureRouteDetailResponse:
    return DepartureRouteDetailResponse.model_validate(detail)


@router.get("/api/departures/here", response_model=StopDepartureSnapshotResponse)
def get_departures_here() -> StopDepartureSnapshotResponse:
    """Return the current kiosk stop departure decisions."""
    try:
        snapshot = get_departure_snapshot_here()
    except DepartureSnapshotUnavailable as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    return _snapshot_to_response(snapshot)


@router.get(
    "/api/departures/routes/{route}/detail",
    response_model=DepartureRouteDetailResponse,
)
def get_departure_route_detail(route: str) -> DepartureRouteDetailResponse:
    """Return structured stop-order details for a route serving this kiosk."""
    try:
        detail = get_route_detail_here(route)
    except RouteDetailNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except RouteDetailUnavailable as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    return _route_detail_to_response(detail)
