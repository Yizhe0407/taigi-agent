from datetime import datetime

from fastapi.testclient import TestClient

import api
import api.departures
import api.moovo
import api.route_plans
from providers import otp
from services.departures import (
    DepartureDecision,
    DepartureRouteDetail,
    DepartureRouteStatus,
    DepartureSection,
    DepartureSnapshotUnavailable,
    DepartureSummary,
    RouteDetailNotFound,
    RouteDirectionDetail,
    RouteStopDetail,
    StopDepartureSnapshot,
)
from services.moovo import MoovoApiError, MoovoStation, NearbyMoovoStation
from services.route_plans import (
    InvalidRouteDestination,
    Place,
    RouteOption,
    RoutePlan,
    RoutePlanningUnavailable,
    RoutePlanNotFound,
)


def _time(value: str) -> datetime:
    return datetime.fromisoformat(f"2026-05-22T{value}:00+08:00")


def _snapshot_time() -> datetime:
    return datetime.fromisoformat("2026-05-24T12:00:00+08:00")


def _departure_snapshot() -> StopDepartureSnapshot:
    routes = (
        DepartureRouteStatus(
            id="65036-2",
            route="201",
            route_id=65036,
            direction="往高鐵雲林站",
            go_back=2,
            section=DepartureSection.AVAILABLE,
            decision=DepartureDecision.CAN_WAIT,
            status_text="約 8 分鐘後",
            decision_text="可以等",
            minutes=8,
            scheduled_time=None,
            sort_priority=10,
                sort_minutes=15,
        ),
        DepartureRouteStatus(
            id="15121-2",
            route="101",
            route_id=15121,
            direction="往斗六棒球場",
            go_back=2,
            section=DepartureSection.LAST_DEPARTED,
            decision=DepartureDecision.LAST_DEPARTED,
            status_text="末班駛離",
            decision_text="末班已過",
            minutes=None,
            scheduled_time=None,
            sort_priority=300,
                sort_minutes=9999,
        ),
    )
    return StopDepartureSnapshot(
        stop_name="雲林科技大學",
        direction_filter=2,
        updated_at=_snapshot_time(),
        routes=routes,
        summary=DepartureSummary(
            available_count=1,
            not_departed_count=0,
            last_departed_count=1,
            unknown_count=0,
        ),
    )


def _departure_route_detail() -> DepartureRouteDetail:
    return DepartureRouteDetail(
        route="201",
        route_id=65036,
        stop_name="雲林科技大學",
        direction_filter=2,
        directions=(
            RouteDirectionDetail(
                go_back=2,
                label="往高鐵雲林站",
                stops=(
                    RouteStopDetail(
                        seq=1,
                        name="雲林科技大學",
                        is_current_stop=True,
                        status_text="即將到站",
                        minutes=0,
                        scheduled_time=None,
                    ),
                    RouteStopDetail(
                        seq=2,
                        name="大學路口",
                        is_current_stop=False,
                        status_text="約 4 分鐘後",
                        minutes=4,
                        scheduled_time=None,
                    ),
                ),
            ),
        ),
    )


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


def _moovo_station() -> MoovoStation:
    return MoovoStation(
        station_uid="YUN100",
        station_id="100",
        name="雲林科技大學",
        latitude=23.696147,
        longitude=120.534823,
        bike_capacity=18,
        available_rent_bikes=6,
        available_return_bikes=4,
        service_status=1,
        update_time=datetime.fromisoformat("2026-05-22T08:30:00+08:00"),
    )


def test_get_departures_here_returns_structured_snapshot(monkeypatch):
    monkeypatch.setattr(
        api.departures,
        "get_departure_snapshot_here",
        lambda: _departure_snapshot(),
    )

    response = TestClient(api.app).get("/api/departures/here")

    assert response.status_code == 200
    assert response.json() == {
        "stopName": "雲林科技大學",
        "directionFilter": 2,
        "updatedAt": "2026-05-24T12:00:00+08:00",
        "summary": {
            "availableCount": 1,
            "notDepartedCount": 0,
            "lastDepartedCount": 1,
            "unknownCount": 0,
        },
        "routes": [
            {
                "id": "65036-2",
                "route": "201",
                "routeId": 65036,
                "direction": "往高鐵雲林站",
                "goBack": 2,
                "section": "available",
                "decision": "can_wait",
                "statusText": "約 8 分鐘後",
                "decisionText": "可以等",
                "minutes": 8,
                "scheduledTime": None,
            },
            {
                "id": "15121-2",
                "route": "101",
                "routeId": 15121,
                "direction": "往斗六棒球場",
                "goBack": 2,
                "section": "last_departed",
                "decision": "last_departed",
                "statusText": "末班駛離",
                "decisionText": "末班已過",
                "minutes": None,
                "scheduledTime": None,
            },
        ],
    }


def test_get_departures_here_maps_provider_errors(monkeypatch):
    def unavailable():
        raise DepartureSnapshotUnavailable("雲林公車查詢失敗：timeout")

    monkeypatch.setattr(api.departures, "get_departure_snapshot_here", unavailable)

    response = TestClient(api.app).get("/api/departures/here")

    assert response.status_code == 503
    assert response.json()["detail"] == "雲林公車查詢失敗：timeout"


def test_get_departure_route_detail_returns_structured_stops(monkeypatch):
    monkeypatch.setattr(
        api.departures,
        "get_route_detail_here",
        lambda route: _departure_route_detail(),
    )

    response = TestClient(api.app).get("/api/departures/routes/201/detail")

    assert response.status_code == 200
    assert response.json() == {
        "route": "201",
        "routeId": 65036,
        "stopName": "雲林科技大學",
        "directionFilter": 2,
        "directions": [
            {
                "goBack": 2,
                "label": "往高鐵雲林站",
                "stops": [
                    {
                        "seq": 1,
                        "name": "雲林科技大學",
                        "isCurrentStop": True,
                        "statusText": "即將到站",
                        "minutes": 0,
                        "scheduledTime": None,
                    },
                    {
                        "seq": 2,
                        "name": "大學路口",
                        "isCurrentStop": False,
                        "statusText": "約 4 分鐘後",
                        "minutes": 4,
                        "scheduledTime": None,
                    },
                ],
            },
        ],
    }


def test_get_departure_route_detail_maps_not_found(monkeypatch):
    def not_found(route):
        raise RouteDetailNotFound(f"在本站找不到停靠路線 {route}")

    monkeypatch.setattr(api.departures, "get_route_detail_here", not_found)

    response = TestClient(api.app).get("/api/departures/routes/999/detail")

    assert response.status_code == 404
    assert response.json()["detail"] == "在本站找不到停靠路線 999"


def test_create_route_plan_returns_frontend_view_model(monkeypatch):
    calls = []

    def fake_plan(latitude, longitude, departure_time):
        calls.append((latitude, longitude, departure_time))
        return _route_plan()

    monkeypatch.setattr(api.route_plans, "plan_route_to_coordinate", fake_plan)

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
    monkeypatch.setattr(
        api.route_plans, "plan_route_to_coordinate", lambda *args: _route_plan()
    )
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

    monkeypatch.setattr(api.route_plans, "plan_route_to_coordinate", no_route)
    not_found = client.post(
        "/api/route-plans",
        json={"destination": {"lat": 23.7178, "lng": 120.5384}},
    )

    def unavailable(*args):
        raise RoutePlanningUnavailable("OTP 路線規劃失敗")

    monkeypatch.setattr(api.route_plans, "plan_route_to_coordinate", unavailable)
    unavailable_response = client.post(
        "/api/route-plans",
        json={"destination": {"lat": 23.7178, "lng": 120.5384}},
    )

    def invalid_destination(*args):
        raise InvalidRouteDestination("目前僅支援雲林縣內目的地")

    monkeypatch.setattr(
        api.route_plans, "plan_route_to_coordinate", invalid_destination
    )
    invalid_response = client.post(
        "/api/route-plans",
        json={"destination": {"lat": 23.480075, "lng": 120.449111}},
    )

    assert not_found.status_code == 404
    assert unavailable_response.status_code == 503
    assert invalid_response.status_code == 400


def test_list_moovo_stations_returns_tdx_availability(monkeypatch):
    monkeypatch.setattr(api.moovo, "load_moovo_stations", lambda: (_moovo_station(),))

    response = TestClient(api.app).get("/api/moovo/stations")

    assert response.status_code == 200
    assert response.json() == {
        "stations": [
            {
                "stationUid": "YUN100",
                "stationId": "100",
                "name": "雲林科技大學",
                "lat": 23.696147,
                "lng": 120.534823,
                "bikeCapacity": 18,
                "availableRentBikes": 6,
                "availableReturnBikes": 4,
                "serviceStatus": 1,
                "updateTime": "2026-05-22T08:30:00+08:00",
            }
        ]
    }


def test_list_nearby_moovo_stations_passes_query(monkeypatch):
    calls = []

    def fake_nearby(latitude, longitude, *, radius_meters, limit):
        calls.append((latitude, longitude, radius_meters, limit))
        return (NearbyMoovoStation(_moovo_station(), 17.5),)

    monkeypatch.setattr(api.moovo, "nearby_moovo_stations", fake_nearby)

    response = TestClient(api.app).get(
        "/api/moovo/stations/nearby?lat=23.696147&lng=120.534823"
        "&radius=800&limit=3"
    )

    assert response.status_code == 200
    assert response.json()["stations"][0]["distanceMeters"] == 17.5
    assert calls == [(23.696147, 120.534823, 800, 3)]


def test_moovo_endpoints_map_provider_errors(monkeypatch):
    def unavailable():
        raise MoovoApiError("TDX Bike request failed")

    monkeypatch.setattr(api.moovo, "load_moovo_stations", unavailable)

    response = TestClient(api.app).get("/api/moovo/stations")

    assert response.status_code == 503
    assert response.json()["detail"] == "TDX Bike request failed"
