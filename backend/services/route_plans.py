"""Kiosk facade for OTP routes to frontend-selected destination coordinates."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import TypedDict
from zoneinfo import ZoneInfo

from providers import otp
from services.kiosk_config import get_kiosk_config
from services.stop_catalog import StopCatalogError, StopRecord, load_stop_catalog
from services.yunlin_boundary import is_in_yunlin_county

_TAIPEI = ZoneInfo("Asia/Taipei")


@dataclass(frozen=True)
class Place:
    name: str
    coordinate: otp.Coordinate


@dataclass(frozen=True)
class RouteOption:
    option_id: str
    itinerary: otp.Itinerary


@dataclass(frozen=True)
class RoutePlan:
    origin: Place
    destination: Place
    routes: tuple[RouteOption, ...]


class RoutePlanningError(RuntimeError):
    """Raised when a frontend-selected Kiosk route cannot be planned."""


class InvalidRouteDestination(RoutePlanningError):
    """Raised when the frontend destination cannot be used by the planner."""


class RoutePlanNotFound(RoutePlanningError):
    """Raised when OTP has no BUS itinerary for the selected destination."""


class RoutePlanningUnavailable(RoutePlanningError):
    """Raised when Kiosk planner dependencies cannot provide route options."""


class PlaceViewModel(TypedDict):
    name: str
    lat: float
    lng: float


class RouteNameViewModel(TypedDict):
    shortName: str | None
    longName: str | None


class LegViewModel(TypedDict):
    mode: str
    fromName: str
    toName: str
    start: str
    end: str
    duration: float
    distance: float
    coordinates: list[list[float]]
    route: RouteNameViewModel | None


class RouteOptionViewModel(TypedDict):
    id: str
    coordinates: list[list[float]]
    duration: int
    distance: float
    transferCount: int
    legs: list[LegViewModel]


class RoutePlanViewModel(TypedDict):
    origin: PlaceViewModel
    destination: PlaceViewModel
    routes: list[RouteOptionViewModel]


_MAX_STOP_SPREAD_DEGREES = 0.02


def _average_coordinate(stops: list[StopRecord]) -> otp.Coordinate | None:
    latitudes = [stop.coordinate.latitude for stop in stops]
    longitudes = [stop.coordinate.longitude for stop in stops]
    if (
        max(latitudes) - min(latitudes) > _MAX_STOP_SPREAD_DEGREES
        or max(longitudes) - min(longitudes) > _MAX_STOP_SPREAD_DEGREES
    ):
        return None
    return otp.Coordinate(
        latitude=sum(latitudes) / len(latitudes),
        longitude=sum(longitudes) / len(longitudes),
    )


def _resolve_place(name: str) -> Place | None:
    """Look up a stop name in the catalog and average nearby stop coordinates.

    Returns None if the name is not found or stops are too geographically spread
    to produce a meaningful single coordinate.
    """
    stop_name = name.strip()
    if not stop_name:
        return None

    catalog = load_stop_catalog()
    exact_stops = catalog.exact(stop_name)
    if not exact_stops:
        return None

    coordinate = _average_coordinate(exact_stops)
    if coordinate is None:
        return None
    return Place(stop_name, coordinate)


def _kiosk_place() -> Place | None:
    """Resolve kiosk origin from runtime config.

    Priority:
    1. Runtime config lat/lon — precise coordinate set via admin UI, bypasses averaging.
    2. Runtime config stop_name — averaged over matching GTFS records (fallback).
    """
    cfg = get_kiosk_config()
    if cfg.lat is not None and cfg.lon is not None:
        if math.isfinite(cfg.lat) and math.isfinite(cfg.lon):
            return Place(
                cfg.stop_name,
                otp.Coordinate(latitude=cfg.lat, longitude=cfg.lon),
            )
    return _resolve_place(cfg.stop_name)


def _destination_place(latitude: float, longitude: float) -> Place | None:
    if (
        not isinstance(latitude, int | float)
        or not isinstance(longitude, int | float)
        or not math.isfinite(latitude)
        or not math.isfinite(longitude)
        or not -90 <= latitude <= 90
        or not -180 <= longitude <= 180
    ):
        return None
    return Place(
        "地圖選點",
        otp.Coordinate(latitude=float(latitude), longitude=float(longitude)),
    )


def _display_leg_names(
    leg: otp.Leg,
    *,
    origin: Place,
    destination: Place,
) -> tuple[str, str]:
    from_name = origin.name if leg.from_name == "Origin" else leg.from_name
    to_name = destination.name if leg.to_name == "Destination" else leg.to_name
    return from_name, to_name


async def plan_route_to_coordinate(
    latitude: float,
    longitude: float,
    departure_time: datetime | None = None,
) -> RoutePlan:
    """Plan BUS route options from the configured Kiosk stop to a map coordinate."""
    try:
        origin = _kiosk_place()
    except StopCatalogError as error:
        raise RoutePlanningUnavailable(f"雲林站牌索引讀取失敗：{error}") from error

    if origin is None:
        raise RoutePlanningUnavailable(
            f"目前無法解析本站「{get_kiosk_config().stop_name}」的路線規劃起點"
        )

    destination_place = _destination_place(latitude, longitude)
    if destination_place is None:
        raise InvalidRouteDestination("目的地座標格式有誤")
    if not is_in_yunlin_county(
        destination_place.coordinate.latitude,
        destination_place.coordinate.longitude,
    ):
        raise InvalidRouteDestination("目前僅支援雲林縣內目的地")

    try:
        itineraries = await otp.plan_bus_connections(
            origin.coordinate,
            destination_place.coordinate,
            departure_time or datetime.now(_TAIPEI),
        )
    except otp.OtpError as error:
        raise RoutePlanningUnavailable(f"OTP 路線規劃失敗：{error}") from error

    bus_itineraries = [plan for plan in itineraries if plan.bus_legs][:3]
    if not bus_itineraries:
        raise RoutePlanNotFound(
            f"找不到從「{origin.name}」到「{destination_place.name}」的公車規劃"
        )

    return RoutePlan(
        origin=origin,
        destination=destination_place,
        routes=tuple(
            RouteOption(f"option-{index}", itinerary)
            for index, itinerary in enumerate(bus_itineraries, start=1)
        ),
    )


def _coordinates_view_model(
    coordinates: tuple[otp.Coordinate, ...],
) -> list[list[float]]:
    return [
        [coordinate.longitude, coordinate.latitude] for coordinate in coordinates
    ]


def _place_view_model(place: Place) -> PlaceViewModel:
    return {
        "name": place.name,
        "lat": place.coordinate.latitude,
        "lng": place.coordinate.longitude,
    }


def _leg_view_model(
    leg: otp.Leg,
    *,
    origin: Place,
    destination: Place,
) -> LegViewModel:
    from_name, to_name = _display_leg_names(
        leg,
        origin=origin,
        destination=destination,
    )
    route: RouteNameViewModel | None = None
    if leg.is_bus:
        route = {
            "shortName": leg.route_short_name,
            "longName": leg.route_long_name,
        }
    return {
        "mode": leg.mode,
        "fromName": from_name,
        "toName": to_name,
        "start": leg.start.isoformat(),
        "end": leg.end.isoformat(),
        "duration": leg.duration_seconds,
        "distance": leg.distance_meters,
        "coordinates": _coordinates_view_model(leg.geometry),
        "route": route,
    }


def route_plan_to_view_model(plan: RoutePlan) -> RoutePlanViewModel:
    """Convert a Kiosk route plan to frontend map route data."""
    routes: list[RouteOptionViewModel] = []
    for route in plan.routes:
        itinerary = route.itinerary
        routes.append(
            {
                "id": route.option_id,
                "coordinates": _coordinates_view_model(itinerary.geometry),
                "duration": itinerary.duration_seconds,
                "distance": itinerary.distance_meters,
                "transferCount": itinerary.transfer_count,
                "legs": [
                    _leg_view_model(
                        leg,
                        origin=plan.origin,
                        destination=plan.destination,
                    )
                    for leg in itinerary.legs
                ],
            }
        )
    return {
        "origin": _place_view_model(plan.origin),
        "destination": _place_view_model(plan.destination),
        "routes": routes,
    }
