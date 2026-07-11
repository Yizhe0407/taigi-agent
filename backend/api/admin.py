"""Admin endpoints: runtime kiosk config + stop catalog for the admin UI."""

from __future__ import annotations

import os
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from services.kiosk_config import KioskConfig, get_kiosk_config, set_kiosk_config
from services.stop_catalog import StopCatalogError, StopRecord, load_stop_catalog

router = APIRouter()


def _require_admin_token(x_admin_token: str | None = Header(default=None)) -> None:
    """Reject kiosk-config writes when ADMIN_TOKEN is set and the header doesn't match.

    ADMIN_TOKEN unset keeps prior (unauthenticated) behavior — opt-in so
    existing LAN-only deployments aren't broken until an operator sets it.
    """
    expected = os.getenv("ADMIN_TOKEN", "")
    if expected and x_admin_token != expected:
        raise HTTPException(status_code=401, detail="缺少或錯誤的管理員權杖")


class KioskConfigResponse(BaseModel):
    stop_name: str
    direction: str | None
    lat: float | None
    lng: float | None


class KioskConfigRequest(BaseModel):
    stop_name: str = Field(min_length=1)
    direction: Literal["去程", "回程"] | None
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)


class StopEntry(BaseModel):
    name: str
    lat: float
    lng: float


def _cfg_to_response(cfg: KioskConfig) -> KioskConfigResponse:
    return KioskConfigResponse(
        stop_name=cfg.stop_name,
        direction=cfg.direction,
        lat=cfg.lat,
        lng=cfg.lon,
    )


def _largest_cluster_centroid(stops: list[StopRecord]) -> tuple[float, float]:
    """Return the centroid of the largest proximity cluster within a stop-name group.

    Groups stops that lie within RADIUS degrees of each other (~111 m at Taiwan's
    latitude). Uses the largest such cluster so a single outlier record cannot pull
    the representative coordinate toward an unrelated physical location.

    For most stop names all records collapse into one cluster (GPS drift < 30 m).
    For names with two distinct locations (e.g. north / south campus entrance)
    the dominant cluster — typically the one with more route stop_ids — wins.
    """
    if len(stops) == 1:
        return stops[0].coordinate.latitude, stops[0].coordinate.longitude

    RADIUS = 0.001  # ≈ 111 m

    best_cluster: list[StopRecord] = []
    for anchor in stops:
        cluster = [
            s
            for s in stops
            if abs(s.coordinate.latitude - anchor.coordinate.latitude) <= RADIUS and abs(s.coordinate.longitude - anchor.coordinate.longitude) <= RADIUS
        ]
        if len(cluster) > len(best_cluster):
            best_cluster = cluster

    lats = [s.coordinate.latitude for s in best_cluster]
    lons = [s.coordinate.longitude for s in best_cluster]
    return sum(lats) / len(lats), sum(lons) / len(lons)


@router.get("/api/admin/kiosk", response_model=KioskConfigResponse)
def get_admin_kiosk() -> KioskConfigResponse:
    """Return the current runtime kiosk configuration."""
    return _cfg_to_response(get_kiosk_config())


@router.put(
    "/api/admin/kiosk",
    response_model=KioskConfigResponse,
    dependencies=[Depends(_require_admin_token)],
)
def update_admin_kiosk(req: KioskConfigRequest) -> KioskConfigResponse:
    """Update the kiosk stop, direction, and coordinates. Takes effect immediately."""
    cfg = KioskConfig(
        stop_name=req.stop_name,
        direction=req.direction,
        lat=req.lat,
        lon=req.lng,
    )
    set_kiosk_config(cfg)
    return _cfg_to_response(cfg)


@router.get("/api/admin/stops", response_model=list[StopEntry])
def list_stops() -> list[StopEntry]:
    """Return one entry per unique stop name for the admin map/search UI.

    Coordinate is the centroid of the largest proximity cluster for that name,
    so GPS-drift duplicates collapse while genuinely distinct locations (> ~111 m
    apart) still produce a stable, representative point.
    """
    try:
        catalog = load_stop_catalog()
    except StopCatalogError as exc:
        raise HTTPException(status_code=503, detail="站牌目錄讀取失敗") from exc

    # Group all stop records by name
    groups: dict[str, list[StopRecord]] = {}
    for s in catalog.stops:
        groups.setdefault(s.name, []).append(s)

    result: list[StopEntry] = []
    for name, stops in sorted(groups.items()):
        lat, lng = _largest_cluster_centroid(stops)
        result.append(StopEntry(name=name, lat=lat, lng=lng))

    return result
