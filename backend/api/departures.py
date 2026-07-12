"""Structured departure decision endpoints."""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from datetime import datetime

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
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
from services.kiosk_config import kiosk_go_back_filter, kiosk_stop_name

router = APIRouter()


# ── Kiosk-scoped service wrappers ─────────────────────────────────────────────
#
# These keep the env-driven kiosk scope at the HTTP boundary so the service
# layer (services/departures.py) stays a pure (stop_name, go_back) function.
# Tests monkeypatch these symbols on `api.departures` so route handlers see
# the patched callable.


async def get_departure_snapshot_here(*, updated_at: datetime | None = None) -> StopDepartureSnapshot:
    return await build_departure_snapshot(
        kiosk_stop_name(),
        kiosk_go_back_filter(),
        updated_at=updated_at,
    )


async def get_route_detail_here(route: str) -> DepartureRouteDetail:
    return await build_route_detail(route, kiosk_stop_name(), kiosk_go_back_filter())


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
    route_id: str = Field(alias="routeId")
    direction: str
    go_back: int = Field(alias="goBack")
    section: DepartureSection
    decision: DepartureDecision
    status_text: str = Field(alias="statusText")
    decision_text: str = Field(alias="decisionText")
    minutes: int | None
    scheduled_time: str | None = Field(alias="scheduledTime")
    car_id: str | None = Field(alias="carId")


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
    route_id: str = Field(alias="routeId")
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


# ── SSE push ──────────────────────────────────────────────────────────────────
#
# The ETA warmup loop (api/__init__._eta_warmup_loop) refreshes the provider
# cache every 25 s and calls notify_snapshot_refreshed(); connected clients
# get a fresh snapshot the moment the backend has one, instead of polling out
# of phase with the cache.

# Fresh Event per tick: set-then-replace means every waiter of the old tick
# wakes exactly once and new waiters latch onto the next tick — no clear() race.
_refresh_event = asyncio.Event()
# Warmup tick is 25 s; if it stalls, degrade to slow self-refresh instead of
# leaving the dashboard frozen.
_STREAM_FALLBACK_SECONDS = 40.0


def notify_snapshot_refreshed() -> None:
    """Wake departure SSE clients after an ETA cache refresh."""
    global _refresh_event
    _refresh_event.set()
    _refresh_event = asyncio.Event()


async def _departure_events():
    """無限 SSE 事件流：snapshot JSON 或 {"error": …}，每次 cache 更新推一筆。"""
    while True:
        # Capture before building so a tick that lands mid-build isn't missed.
        wakeup = _refresh_event
        try:
            snapshot = await get_departure_snapshot_here()
            payload = _snapshot_to_response(snapshot).model_dump_json(by_alias=True)
        except DepartureSnapshotUnavailable as error:
            payload = json.dumps({"error": str(error)}, ensure_ascii=False)
        yield f"data: {payload}\n\n"
        with suppress(TimeoutError):
            await asyncio.wait_for(wakeup.wait(), timeout=_STREAM_FALLBACK_SECONDS)


@router.get("/api/departures/stream")
async def stream_departures_here() -> StreamingResponse:
    """SSE：每次 ETA cache 更新即推最新 snapshot。"""
    return StreamingResponse(
        _departure_events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


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
