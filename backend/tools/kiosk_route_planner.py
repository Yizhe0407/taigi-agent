"""Kiosk facade for OTP routes to frontend-selected destination coordinates."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from datetime import datetime
from typing import TypedDict
from zoneinfo import ZoneInfo

from tools import otp
from tools.stop_catalog import StopCatalogError, StopRecord, load_stop_catalog

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


_ALIASES: dict[str, str] = {
    "雲科": "雲林科技大學",
    "雲科大": "雲林科技大學",
}
_MAX_STOP_SPREAD_DEGREES = 0.02


def _known_name(name: str) -> str:
    return _ALIASES.get(name.strip(), name.strip())


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
    stop_name = _known_name(name)
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
    return _resolve_place(os.getenv("KIOSK_STOP", "雲林科技大學"))


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


def _minutes(start: datetime, end: datetime) -> int:
    seconds = max(0, int((end - start).total_seconds()))
    return (seconds + 59) // 60


def _format_time(value: datetime) -> str:
    return value.astimezone(_TAIPEI).strftime("%H:%M")


def _format_leg(
    index: int,
    leg: otp.Leg,
    *,
    origin: Place,
    destination: Place,
) -> str | None:
    from_name, to_name = _display_leg_names(
        leg,
        origin=origin,
        destination=destination,
    )
    if leg.is_bus:
        route = leg.route_short_name or leg.route_long_name or "未知路線"
        return (
            f"{index}. 搭 {route}：{from_name} -> {to_name}"
            f"（預定 {_format_time(leg.start)} -> {_format_time(leg.end)}）"
        )
    if from_name == to_name:
        return None
    minutes = _minutes(leg.start, leg.end)
    if leg.from_name == "Origin":
        return f"{index}. 步行到 {to_name}（約 {minutes} 分鐘）"
    if leg.to_name == "Destination":
        return f"{index}. 從 {from_name} 步行到 {to_name}（約 {minutes} 分鐘）"
    return f"{index}. 步行：{from_name} -> {to_name}（約 {minutes} 分鐘）"


def _display_leg_names(
    leg: otp.Leg,
    *,
    origin: Place,
    destination: Place,
) -> tuple[str, str]:
    from_name = origin.name if leg.from_name == "Origin" else leg.from_name
    to_name = destination.name if leg.to_name == "Destination" else leg.to_name
    return from_name, to_name


def _format_itinerary(
    index: int,
    itinerary: otp.Itinerary,
    *,
    origin: Place,
    destination: Place,
) -> list[str]:
    transfer_label = (
        "不用轉乘"
        if itinerary.transfer_count == 0
        else f"轉乘 {itinerary.transfer_count} 次"
    )
    lines = [
        f"方案 {index}：約 {itinerary.duration_minutes} 分鐘，{transfer_label}"
    ]
    for leg in itinerary.legs:
        line = _format_leg(
            len(lines),
            leg,
            origin=origin,
            destination=destination,
        )
        if line is not None:
            lines.append(line)
    return lines


def plan_route_to_coordinate(
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
        stop = os.getenv("KIOSK_STOP", "雲林科技大學")
        raise RoutePlanningUnavailable(f"目前無法解析本站「{stop}」的路線規劃起點")

    destination_place = _destination_place(latitude, longitude)
    if destination_place is None:
        raise InvalidRouteDestination("目的地座標格式有誤")

    try:
        itineraries = otp.plan_bus_connections(
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


def format_route_plan(plan: RoutePlan) -> str:
    """Format a structured route plan for CLI inspection or short summaries."""
    lines = [f"從「{plan.origin.name}」到「{plan.destination.name}」的公車規劃："]
    for index, route in enumerate(plan.routes, start=1):
        lines.extend(
            _format_itinerary(
                index,
                route.itinerary,
                origin=plan.origin,
                destination=plan.destination,
            )
        )
    return "\n".join(lines)


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
