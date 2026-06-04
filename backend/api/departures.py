"""Structured departure decision endpoints."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from services.departures import (
    DepartureDecision,
    DepartureRouteDetail,
    DepartureSection,
    DepartureSnapshotUnavailable,
    RouteDetailNotFound,
    RouteDetailUnavailable,
    StopDepartureSnapshot,
    build_departure_snapshot,
    build_route_detail,
)
from services.kiosk_config import get_kiosk_config


def _kiosk_stop() -> str:
    return get_kiosk_config().stop_name


def _kiosk_go_back_filter() -> int | None:
    return get_kiosk_config().go_back

router = APIRouter()


# ── Kiosk-scoped service wrappers ─────────────────────────────────────────────
#
# These keep the env-driven kiosk scope at the HTTP boundary so the service
# layer (services/departures.py) stays a pure (stop_name, go_back) function.
# Tests monkeypatch these symbols on `api.departures` so route handlers see
# the patched callable.

async def get_departure_snapshot_here(
    *, updated_at: datetime | None = None
) -> StopDepartureSnapshot:
    return await build_departure_snapshot(
        _kiosk_stop(),
        _kiosk_go_back_filter(),
        updated_at=updated_at,
    )


async def get_route_detail_here(route: str) -> DepartureRouteDetail:
    return await build_route_detail(route, _kiosk_stop(), _kiosk_go_back_filter())


# ── Pydantic response schemas ─────────────────────────────────────────────────


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
async def get_departures_here() -> StopDepartureSnapshotResponse:
    """Return the current kiosk stop departure decisions."""
    try:
        snapshot = await get_departure_snapshot_here()
    except DepartureSnapshotUnavailable as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    return _snapshot_to_response(snapshot)


@router.get(
    "/api/departures/routes/{route}/detail",
    response_model=DepartureRouteDetailResponse,
)
async def get_departure_route_detail(route: str) -> DepartureRouteDetailResponse:
    """Return structured stop-order details for a route serving this kiosk."""
    try:
        detail = await get_route_detail_here(route)
    except RouteDetailNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except RouteDetailUnavailable as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    return _route_detail_to_response(detail)
