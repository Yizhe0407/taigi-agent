"""OpenTripPlanner GTFS GraphQL provider for bus route planning."""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import polyline
import requests

_DEFAULT_BASE_URL = "http://localhost:8081"
_GRAPHQL_PATH = "/otp/gtfs/v1"
_TIMEOUT_SECONDS = 15


class OtpError(RuntimeError):
    """Raised when OTP cannot return a usable plan."""


@dataclass(frozen=True)
class Coordinate:
    latitude: float
    longitude: float


@dataclass(frozen=True)
class Leg:
    mode: str
    from_name: str
    to_name: str
    start: datetime
    end: datetime
    route_short_name: str | None = None
    route_long_name: str | None = None
    duration_seconds: float = 0
    distance_meters: float = 0
    geometry: tuple[Coordinate, ...] = ()

    @property
    def is_bus(self) -> bool:
        return self.mode == "BUS"


@dataclass(frozen=True)
class Itinerary:
    start: datetime
    end: datetime
    legs: tuple[Leg, ...]

    @property
    def duration_seconds(self) -> int:
        return max(0, int((self.end - self.start).total_seconds()))

    @property
    def duration_minutes(self) -> int:
        return (self.duration_seconds + 59) // 60

    @property
    def distance_meters(self) -> float:
        return sum(leg.distance_meters for leg in self.legs)

    @property
    def geometry(self) -> tuple[Coordinate, ...]:
        coordinates: list[Coordinate] = []
        for leg in self.legs:
            for coordinate in leg.geometry:
                if not coordinates or coordinate != coordinates[-1]:
                    coordinates.append(coordinate)
        return tuple(coordinates)

    @property
    def bus_legs(self) -> tuple[Leg, ...]:
        return tuple(leg for leg in self.legs if leg.is_bus)

    @property
    def transfer_count(self) -> int:
        return max(0, len(self.bus_legs) - 1)


def _endpoint() -> str:
    base_url = os.getenv("OTP_BASE_URL", _DEFAULT_BASE_URL).rstrip("/")
    return f"{base_url}{_GRAPHQL_PATH}"


def _iso_literal(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("OTP departure_time must include a timezone")
    return json.dumps(value.isoformat())


def _plan_query(
    origin: Coordinate, destination: Coordinate, departure_time: datetime
) -> str:
    return f"""
    {{
      planConnection(
        origin: {{
          location: {{
            coordinate: {{
              latitude: {origin.latitude}
              longitude: {origin.longitude}
            }}
          }}
        }}
        destination: {{
          location: {{
            coordinate: {{
              latitude: {destination.latitude}
              longitude: {destination.longitude}
            }}
          }}
        }}
        dateTime: {{ earliestDeparture: {_iso_literal(departure_time)} }}
        modes: {{
          direct: [WALK]
          transit: {{ transit: [{{ mode: BUS }}] }}
        }}
      ) {{
        edges {{
          node {{
            start
            end
            legs {{
              mode
              duration
              distance
              legGeometry {{
                points
              }}
              from {{
                name
                departure {{ scheduledTime }}
              }}
              to {{
                name
                arrival {{ scheduledTime }}
              }}
              route {{
                shortName
                longName
              }}
            }}
          }}
        }}
      }}
    }}
    """


def _parse_datetime(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise OtpError(f"OTP plan is missing {field}")
    try:
        return datetime.fromisoformat(value)
    except ValueError as error:
        raise OtpError(f"OTP plan has invalid {field}: {value}") from error


def _parse_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise OtpError(f"OTP plan is missing {field}")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise OtpError(f"OTP plan has invalid {field}: {value}")
    return number


def _nested_value(data: dict[str, Any], *keys: str) -> Any:
    current: Any = data
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _parse_geometry(data: dict[str, Any]) -> tuple[Coordinate, ...]:
    geometry = data.get("legGeometry")
    if geometry is None:
        return ()
    points = _nested_value(geometry, "points")
    if not isinstance(points, str):
        raise OtpError("OTP plan leg geometry is missing encoded points")
    try:
        decoded = polyline.decode(points)
    except ValueError as error:
        raise OtpError("OTP plan leg geometry is not a valid polyline") from error
    return tuple(
        Coordinate(latitude=latitude, longitude=longitude)
        for latitude, longitude in decoded
    )


def _parse_leg(data: Any) -> Leg:
    if not isinstance(data, dict):
        raise OtpError("OTP plan has invalid leg data")

    mode = data.get("mode")
    from_name = _nested_value(data, "from", "name")
    to_name = _nested_value(data, "to", "name")
    if not isinstance(mode, str) or not isinstance(from_name, str) or not isinstance(
        to_name, str
    ):
        raise OtpError("OTP plan leg is missing mode or stop name")

    route = data.get("route")
    route_short_name = route.get("shortName") if isinstance(route, dict) else None
    route_long_name = route.get("longName") if isinstance(route, dict) else None
    return Leg(
        mode=mode,
        from_name=from_name,
        to_name=to_name,
        start=_parse_datetime(
            _nested_value(data, "from", "departure", "scheduledTime"),
            "leg departure time",
        ),
        end=_parse_datetime(
            _nested_value(data, "to", "arrival", "scheduledTime"),
            "leg arrival time",
        ),
        route_short_name=(
            route_short_name if isinstance(route_short_name, str) else None
        ),
        route_long_name=route_long_name if isinstance(route_long_name, str) else None,
        duration_seconds=_parse_number(data.get("duration"), "leg duration"),
        distance_meters=_parse_number(data.get("distance"), "leg distance"),
        geometry=_parse_geometry(data),
    )


def _parse_itinerary(data: Any) -> Itinerary:
    if not isinstance(data, dict):
        raise OtpError("OTP plan has invalid itinerary data")
    legs = data.get("legs")
    if not isinstance(legs, list):
        raise OtpError("OTP plan itinerary is missing legs")
    return Itinerary(
        start=_parse_datetime(data.get("start"), "itinerary start time"),
        end=_parse_datetime(data.get("end"), "itinerary end time"),
        legs=tuple(_parse_leg(leg) for leg in legs),
    )


def _parse_plan_response(payload: Any) -> list[Itinerary]:
    if not isinstance(payload, dict):
        raise OtpError("OTP response is not a JSON object")

    errors = payload.get("errors")
    if isinstance(errors, list) and errors:
        first_error = errors[0]
        if isinstance(first_error, dict) and isinstance(
            first_error.get("message"), str
        ):
            raise OtpError(f"OTP GraphQL query failed: {first_error['message']}")
        raise OtpError("OTP GraphQL query failed")

    edges = _nested_value(payload, "data", "planConnection", "edges")
    if not isinstance(edges, list):
        raise OtpError("OTP response is missing planConnection edges")

    itineraries: list[Itinerary] = []
    for edge in edges:
        node = _nested_value(edge, "node")
        if node is not None:
            itineraries.append(_parse_itinerary(node))
    return itineraries


def _post_graphql(query: str) -> Any:
    try:
        response = requests.post(
            _endpoint(),
            json={"query": query},
            timeout=_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
    except requests.RequestException as error:
        raise OtpError(f"OTP request failed: {error}") from error

    try:
        return response.json()
    except ValueError as error:
        raise OtpError("OTP response is not valid JSON") from error


def plan_bus_connections(
    origin: Coordinate,
    destination: Coordinate,
    departure_time: datetime,
) -> list[Itinerary]:
    """Fetch BUS + WALK route candidates from OTP."""
    payload = _post_graphql(_plan_query(origin, destination, departure_time))
    return _parse_plan_response(payload)
