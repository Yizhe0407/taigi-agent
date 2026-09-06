"""Structured departure decision endpoints."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import suppress
from datetime import datetime

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from async_lifecycle import create_lifecycle_task, join_task
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

# No RateLimit on the GET endpoints below: they're the kiosk's high-frequency
# primary path, and services.departures already caches the provider snapshot
# for 25 s, so there's no per-request upstream cost to protect against.
# `/stream` still needs admission control (see `_MAX_STREAM_CONNECTIONS`)
# since rate limiting bounds request frequency, not concurrently open
# long-lived SSE connections.


# ── Kiosk-scoped service wrappers ─────────────────────────────────────────────
#
# Keep the env-driven kiosk scope at the HTTP boundary so services/departures.py
# stays a pure (stop_name, go_back) function. Tests monkeypatch these symbols
# on `api.departures` so route handlers see the patched callable.


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
# api/__init__._eta_warmup_loop refreshes the provider cache every 25 s and
# calls notify_snapshot_refreshed(); connected clients get a fresh snapshot
# the moment it's ready instead of polling out of phase with the cache.

# Warmup tick is 25 s; if it stalls, fall back to this instead of freezing.
_STREAM_FALLBACK_SECONDS = 40.0
# `/stream` is public and indefinitely open.  The runtime owns a bounded set of
# stream leases so every generator that actually starts is accounted for until
# its `finally` releases the lease.
_MAX_STREAM_CONNECTIONS = 200


class _DepartureStreamCapacityError(RuntimeError):
    """Raised when capacity changed after response headers were prepared."""


class _DepartureStreamLease:
    """One stream strongly registered with its app-generation runtime."""

    __slots__ = ("_owner", "_released")

    def __init__(self, owner: _DepartureStreamRuntime) -> None:
        self._owner = owner
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._owner._release(self)


class _DepartureStreamRuntime:
    """Authoritative owner for one lifespan's refresh signal and SSE leases.

    Shutdown permanently closes admission, wakes every stream parked on the
    current refresh generation, and waits until every generator that crossed
    the gate has executed its release.  A late notifier can only address the
    currently installed runtime; it can never create a replacement generation.
    """

    def __init__(self, max_connections: int) -> None:
        self._max_connections = max_connections
        self._refresh_event = asyncio.Event()
        self._leases: set[_DepartureStreamLease] = set()
        self._leases_empty = asyncio.Event()
        self._leases_empty.set()
        self._closing = False
        self._closed = False
        self._shutdown_task: asyncio.Task[None] | None = None

    @property
    def closing(self) -> bool:
        return self._closing

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def active_count(self) -> int:
        return len(self._leases)

    @property
    def max_connections(self) -> int:
        return self._max_connections

    @property
    def capacity_available(self) -> bool:
        return not self._closing and self.active_count < self._max_connections

    def capture_refresh(self) -> asyncio.Event:
        return self._refresh_event

    def notify_snapshot_refreshed(self) -> None:
        if self._closing:
            return
        # Set-then-replace semantics without clear(): every waiter holding the
        # old event wakes exactly once and later waiters capture the next tick.
        wakeup = self._refresh_event
        self._refresh_event = asyncio.Event()
        wakeup.set()

    def acquire(self) -> _DepartureStreamLease:
        if self._closing:
            raise RuntimeError("Departure stream runtime is shutting down")
        if self.active_count >= self._max_connections:
            raise _DepartureStreamCapacityError("Departure stream capacity is exhausted")
        lease = _DepartureStreamLease(self)
        self._leases.add(lease)
        self._leases_empty.clear()
        return lease

    def _release(self, lease: _DepartureStreamLease) -> None:
        self._leases.discard(lease)
        if not self._leases:
            self._leases_empty.set()

    async def _finalize(self) -> None:
        await self._leases_empty.wait()

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closing = True
        # A waiting stream captures the event before yielding its current
        # snapshot.  Setting that exact generation lets it observe the closed
        # gate and retire instead of sleeping through shutdown.
        self._refresh_event.set()

        task = self._shutdown_task
        if task is None:
            task = create_lifecycle_task(
                self._finalize(),
                name="departure-streams-shutdown",
            )
            self._shutdown_task = task

        try:
            await join_task(task)
        finally:
            if self._shutdown_task is task and task.done():
                if not task.cancelled() and task.exception() is None:
                    self._closed = True
                self._shutdown_task = None


_departure_runtime: _DepartureStreamRuntime | None = None


async def startup_departure_streams() -> None:
    """Install a fresh stream generation only after the previous one retired."""
    global _departure_runtime
    previous = _departure_runtime
    if previous is not None:
        await previous.aclose()
    _departure_runtime = _DepartureStreamRuntime(_MAX_STREAM_CONNECTIONS)


async def shutdown_departure_streams() -> None:
    """Close admission and retain the runtime globally until teardown succeeds."""
    global _departure_runtime
    runtime = _departure_runtime
    if runtime is None:
        return
    try:
        await runtime.aclose()
    finally:
        if _departure_runtime is runtime and runtime.closed:
            _departure_runtime = None


def notify_snapshot_refreshed() -> None:
    """Wake this app generation's clients without reviving a closed runtime."""
    runtime = _departure_runtime
    if runtime is not None:
        runtime.notify_snapshot_refreshed()


async def _departure_stream(runtime: _DepartureStreamRuntime) -> AsyncIterator[str]:
    """Yield snapshots while holding one runtime-owned connection lease.

    The acquire must be the *first* statement in the body, before any
    `await`. `StreamingResponse` sends headers (committing to status 200)
    before it's guaranteed to ever drive this generator — on an immediate
    disconnect the body may never start executing, and an async generator
    that never started runs no code on `aclose()` (its `finally` never
    fires). So acquiring anywhere outside this function risks a slot that's
    taken but never freed — a permanent leak. Keeping "body started" and
    "slot acquired" the same event is what guarantees the release always
    matches.

    `stream_departures_here` checks capacity before returning the response so
    the normal full-capacity path gets a precise 503.  The authoritative
    acquire repeats the check here: concurrent responses prepared before
    either body starts may not exceed the hard cap after headers are sent.
    """
    lease = runtime.acquire()
    try:
        while not runtime.closing:
            # Capture before building so a tick that lands mid-build is not
            # missed.  Shutdown also sets this captured generation.
            wakeup = runtime.capture_refresh()
            try:
                snapshot = await get_departure_snapshot_here()
                payload = _snapshot_to_response(snapshot).model_dump_json(by_alias=True)
            except DepartureSnapshotUnavailable as error:
                payload = json.dumps({"error": str(error)}, ensure_ascii=False)

            if runtime.closing:
                return
            yield sse_event(payload)
            if runtime.closing:
                return
            with suppress(TimeoutError):
                await asyncio.wait_for(wakeup.wait(), timeout=_STREAM_FALLBACK_SECONDS)
    finally:
        lease.release()


@router.get("/api/departures/stream")
async def stream_departures_here() -> StreamingResponse:
    """SSE：每次 ETA cache 更新即推最新 snapshot。"""
    runtime = _departure_runtime
    if runtime is None or runtime.closing:
        raise HTTPException(status_code=503, detail="離站串流服務正在關閉，請稍後再試")
    if not runtime.capacity_available:
        raise HTTPException(
            status_code=503,
            detail=f"目前串流連線數已達上限（{runtime.max_connections}），請稍後再試",
        )
    return StreamingResponse(
        _departure_stream(runtime),
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
