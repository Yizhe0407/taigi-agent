from datetime import datetime

import pytest

from tools import kiosk_route_planner, otp
from tools.stop_catalog import StopCatalog, StopRecord


def _time(value: str) -> datetime:
    return datetime.fromisoformat(f"2026-05-22T{value}:00+08:00")


def _stop(stop_id: str, name: str, latitude: float, longitude: float) -> StopRecord:
    return StopRecord(stop_id, name, otp.Coordinate(latitude, longitude))


def _use_catalog(monkeypatch, *stops: StopRecord) -> None:
    monkeypatch.setattr(
        kiosk_route_planner,
        "load_stop_catalog",
        lambda: StopCatalog(stops),
    )


def test_plan_route_to_coordinate_formats_bus_itineraries(monkeypatch):
    itinerary = otp.Itinerary(
        start=_time("08:34"),
        end=_time("08:45"),
        legs=(
            otp.Leg(
                mode="WALK",
                from_name="Origin",
                to_name="雲林科技大學",
                start=_time("08:34"),
                end=_time("08:35"),
                duration_seconds=60,
                distance_meters=80,
                geometry=(
                    otp.Coordinate(23.696, 120.5337),
                    otp.Coordinate(23.69602, 120.533793),
                ),
            ),
            otp.Leg(
                mode="BUS",
                from_name="雲林科技大學",
                to_name="斗六火車站",
                start=_time("08:35"),
                end=_time("08:45"),
                route_short_name="201",
                route_long_name="201",
                duration_seconds=600,
                distance_meters=2500,
                geometry=(
                    otp.Coordinate(23.69602, 120.533793),
                    otp.Coordinate(23.71192, 120.540291),
                ),
            ),
        ),
    )
    walking_only = otp.Itinerary(
        start=_time("08:00"),
        end=_time("08:30"),
        legs=(
            otp.Leg(
                mode="WALK",
                from_name="Origin",
                to_name="Destination",
                start=_time("08:00"),
                end=_time("08:30"),
            ),
        ),
    )

    monkeypatch.setenv("KIOSK_STOP", "雲科")
    _use_catalog(
        monkeypatch,
        _stop("YUN-NYUST", "雲林科技大學", 23.69602, 120.533793),
    )
    monkeypatch.setattr(
        kiosk_route_planner.otp,
        "plan_bus_connections",
        lambda *args: [walking_only, itinerary],
    )

    plan = kiosk_route_planner.plan_route_to_coordinate(
        23.71192,
        120.540291,
        departure_time=_time("08:00"),
    )

    assert kiosk_route_planner.format_route_plan(plan) == (
        "從「雲林科技大學」到「地圖選點」的公車規劃：\n"
        "方案 1：約 11 分鐘，不用轉乘\n"
        "1. 搭 201：雲林科技大學 -> 斗六火車站（預定 08:35 -> 08:45）"
    )
    route = kiosk_route_planner.route_plan_to_view_model(plan)["routes"][0]
    assert route["id"] == "option-1"
    assert route["coordinates"] == [
        [120.5337, 23.696],
        [120.533793, 23.69602],
        [120.540291, 23.71192],
    ]
    assert route["duration"] == 660
    assert route["distance"] == 2580
    assert route["transferCount"] == 0
    bus_route = route["legs"][1]["route"]
    assert bus_route is not None
    assert bus_route["shortName"] == "201"


def test_plan_route_to_coordinate_rejects_invalid_destination(monkeypatch):
    _use_catalog(
        monkeypatch,
        _stop("YUN-NYUST", "雲林科技大學", 23.69602, 120.533793),
    )

    with pytest.raises(
        kiosk_route_planner.RoutePlanningError,
        match="目的地座標格式有誤",
    ):
        kiosk_route_planner.plan_route_to_coordinate(91, 120)


def test_plan_route_to_coordinate_rejects_out_of_yunlin_destination(monkeypatch):
    _use_catalog(
        monkeypatch,
        _stop("YUN-NYUST", "雲林科技大學", 23.69602, 120.533793),
    )
    monkeypatch.setattr(
        otp,
        "plan_bus_connections",
        lambda *args: pytest.fail("OTP should not be called for out-of-area points"),
    )

    with pytest.raises(
        kiosk_route_planner.RoutePlanningError,
        match="目前僅支援雲林縣內目的地",
    ):
        kiosk_route_planner.plan_route_to_coordinate(23.480075, 120.449111)


def test_resolve_place_uses_exact_kiosk_stop_name(monkeypatch):
    _use_catalog(
        monkeypatch,
        _stop("THB249193", "虎尾", 23.709539, 120.434305),
        _stop("THB248875", "虎尾圓環", 23.71019, 120.43696),
        _stop("THB249194", "虎尾", 23.70946, 120.434022),
    )

    assert kiosk_route_planner._resolve_place("虎尾") == kiosk_route_planner.Place(
        "虎尾",
        otp.Coordinate(latitude=23.7094995, longitude=120.4341635),
    )


def test_resolve_place_only_uses_indexed_stops(monkeypatch):
    _use_catalog(
        monkeypatch,
        _stop("YUN-NYUST", "雲林科技大學", 23.69602, 120.533793),
    )

    assert kiosk_route_planner._resolve_place("臺北轉運站") is None
