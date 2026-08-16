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

from .sse import SSE_HEADERS, sse_event

router = APIRouter()

# Neither GET endpoint below carries a RateLimit dependency — this is
# intentional, not an oversight. `/api/departures/here` and `/stream` are the
# kiosk's own high-frequency primary path (SSE plus polling fallback), and
# `services.departures` already caches the underlying provider snapshot for
# 25 s, so there's no per-request upstream cost to protect against. `/stream`
# does still need admission control though — see `_MAX_STREAM_CONNECTIONS`
# below: rate limiting bounds request *frequency*, not concurrently open
# long-lived connections, which is the actual resource an SSE endpoint spends
# (one asyncio task + one generator each, each rebuilding a full snapshot at
# least every 40 s).


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
        yield sse_event(payload)
        with suppress(TimeoutError):
            await asyncio.wait_for(wakeup.wait(), timeout=_STREAM_FALLBACK_SECONDS)


# ── Concurrent-connection admission control ────────────────────────────────
#
# `/stream` has no per-request RateLimit (see comment above) but is a public,
# indefinitely-open SSE endpoint with no cap otherwise — hundreds of parallel
# connections would each hold an asyncio task + generator and each rebuild a
# full snapshot at least every `_STREAM_FALLBACK_SECONDS`. Cap set far above
# real usage: the kiosk itself only ever opens a handful of `/stream`
# connections (dashboard tab, occasional phone preview during testing), so
# 200 gives two orders of magnitude of headroom — normal use should never
# come close.
_MAX_STREAM_CONNECTIONS = 200
_active_stream_connections = 0


def _stream_capacity_available() -> bool:
    return _active_stream_connections < _MAX_STREAM_CONNECTIONS


def _release_stream_slot() -> None:
    global _active_stream_connections
    _active_stream_connections -= 1


async def _admission_controlled_events():
    """Occupy a connection slot for the lifetime of this generator.

    The acquire is the *first* statement in the body, before any `await`,
    paired with a `finally` release wrapping everything else. This is
    deliberately NOT split into "endpoint acquires, generator releases":
    Starlette's `StreamingResponse` sends response headers (committing to
    status 200) *before* it is guaranteed to ever drive this generator with
    `__anext__` — on an immediate client disconnect, its collapsing task
    group can cancel between sending headers and the first iteration (or
    `send()` itself can raise), so the generator body may never start
    executing. An async generator whose body never started runs no code on
    `aclose()` — its `finally` never fires — so acquiring a slot anywhere
    outside this function risks a slot that's taken but never freed
    (permanent leak → endpoint eventually wedges into permanent 503, worse
    than having no limit at all). Making the acquire itself the first thing
    the body does means "never started" and "never acquired" are the same
    event, so there is no way to hold a slot without the matching release
    also being guaranteed.

    Trade-off: `stream_departures_here` only *checks* capacity before
    returning `StreamingResponse` (a precise 503 needs that check before
    headers are sent, since status can't change after); the actual acquire
    happens here, slightly later. Between that check and this acquire, other
    concurrently arriving requests can pass the same check, so a burst can
    briefly push the real count a little over `_MAX_STREAM_CONNECTIONS` —
    bounded only by how many requests land in that window, and
    self-correcting once they've all started. At a limit set two orders of
    magnitude above real usage that slack is a non-issue; it's a much better
    trade than an endpoint that can permanently wedge itself into 503 under
    churn of quick-disconnecting clients.
    """
    global _active_stream_connections
    _active_stream_connections += 1
    try:
        async for event in _departure_events():
            yield event
    finally:
        _release_stream_slot()


@router.get("/api/departures/stream")
async def stream_departures_here() -> StreamingResponse:
    """SSE：每次 ETA cache 更新即推最新 snapshot。"""
    if not _stream_capacity_available():
        raise HTTPException(
            status_code=503,
            detail=f"目前串流連線數已達上限（{_MAX_STREAM_CONNECTIONS}），請稍後再試",
        )
    return StreamingResponse(
        _admission_controlled_events(),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
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
