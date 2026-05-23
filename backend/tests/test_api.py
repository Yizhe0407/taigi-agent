from datetime import datetime

from fastapi.testclient import TestClient

import api
from tools import otp
from tools.kiosk_route_planner import (
    Place,
    RouteOption,
    RoutePlan,
    RoutePlanningUnavailable,
    RoutePlanNotFound,
)


def _time(value: str) -> datetime:
    return datetime.fromisoformat(f"2026-05-22T{value}:00+08:00")


def _route_plan() -> RoutePlan:
    return RoutePlan(
        origin=Place("雲林科技大學", otp.Coordinate(23.69602, 120.533793)),
        destination=Place("地圖選點", otp.Coordinate(23.717832, 120.538408)),
        routes=(
            RouteOption(
                "option-1",
                otp.Itinerary(
                    start=_time("08:34"),
                    end=_time("08:45"),
                    legs=(
                        otp.Leg(
                            mode="BUS",
                            from_name="Origin",
                            to_name="Destination",
                            start=_time("08:34"),
                            end=_time("08:45"),
                            route_short_name="201",
                            route_long_name="201",
                            duration_seconds=660,
                            distance_meters=2580,
                            geometry=(
                                otp.Coordinate(23.69602, 120.533793),
                                otp.Coordinate(23.717832, 120.538408),
                            ),
                        ),
                    ),
                ),
            ),
        ),
    )


def test_create_route_plan_returns_frontend_view_model(monkeypatch):
    calls = []

    def fake_plan(latitude, longitude, departure_time):
        calls.append((latitude, longitude, departure_time))
        return _route_plan()

    monkeypatch.setattr(api, "plan_route_to_coordinate", fake_plan)

    response = TestClient(api.app).post(
        "/api/route-plans",
        json={
            "destination": {
                "lat": 23.717831598831527,
                "lng": 120.53840824484192,
            },
            "departureTime": "2026-05-22T08:00:00+08:00",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["origin"]["name"] == "雲林科技大學"
    assert payload["routes"][0]["coordinates"] == [
        [120.533793, 23.69602],
        [120.538408, 23.717832],
    ]
    assert payload["routes"][0]["legs"][0]["route"]["shortName"] == "201"
    assert calls == [
        (
            23.717831598831527,
            120.53840824484192,
            datetime.fromisoformat("2026-05-22T08:00:00+08:00"),
        )
    ]


def test_create_route_plan_validates_destination_and_departure_time(monkeypatch):
    monkeypatch.setattr(api, "plan_route_to_coordinate", lambda *args: _route_plan())
    client = TestClient(api.app)

    invalid_destination = client.post(
        "/api/route-plans",
        json={"destination": {"lat": 91, "lng": 120}},
    )
    invalid_departure = client.post(
        "/api/route-plans",
        json={
            "destination": {"lat": 23.7178, "lng": 120.5384},
            "departureTime": "2026-05-22T08:00:00",
        },
    )

    assert invalid_destination.status_code == 422
    assert invalid_departure.status_code == 422


def test_create_route_plan_maps_route_errors(monkeypatch):
    client = TestClient(api.app)

    def no_route(*args):
        raise RoutePlanNotFound("找不到公車規劃")

    monkeypatch.setattr(api, "plan_route_to_coordinate", no_route)
    not_found = client.post(
        "/api/route-plans",
        json={"destination": {"lat": 23.7178, "lng": 120.5384}},
    )

    def unavailable(*args):
        raise RoutePlanningUnavailable("OTP 路線規劃失敗")

    monkeypatch.setattr(api, "plan_route_to_coordinate", unavailable)
    unavailable_response = client.post(
        "/api/route-plans",
        json={"destination": {"lat": 23.7178, "lng": 120.5384}},
    )

    assert not_found.status_code == 404
    assert unavailable_response.status_code == 503
