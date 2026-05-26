from datetime import datetime

import pytest

from tools import kiosk_departures, yunlin_ebus


def _updated_at() -> datetime:
    return datetime.fromisoformat("2026-05-24T12:00:00+08:00")


def test_build_departure_snapshot_classifies_and_sorts_routes(monkeypatch):
    route_info = {
        "201": {
            "id": 65036,
            "go_dest": "雲林科技大學",
            "back_dest": "高鐵雲林站",
        },
        "301": {
            "id": 301,
            "go_dest": "雲林科技大學",
            "back_dest": "斗六",
        },
        "302": {
            "id": 302,
            "go_dest": "雲林科技大學",
            "back_dest": "虎尾",
        },
        "7101": {
            "id": 7101,
            "go_dest": "雲林科技大學",
            "back_dest": "麥寮",
        },
        "7000B": {
            "id": 65352,
            "go_dest": "台北站",
            "back_dest": "梅山站",
        },
        "101": {
            "id": 15121,
            "go_dest": "受天宮",
            "back_dest": "斗六棒球場",
        },
    }
    eta_data = [
        {"xno": 302, "GoBack": 2, "Value": 25, "ComeTime": ""},
        {"xno": 15121, "GoBack": 2, "Value": -3, "ComeTime": ""},
        {"xno": 65036, "GoBack": 2, "Value": 2, "ComeTime": ""},
        {"xno": 65352, "GoBack": 2, "Value": None, "ComeTime": ""},
        {"xno": 301, "GoBack": 2, "Value": 12, "ComeTime": ""},
        {"xno": 7101, "GoBack": 2, "Value": None, "ComeTime": "21:35"},
    ]

    monkeypatch.setattr(yunlin_ebus, "_load_route_info", lambda stop: route_info)
    monkeypatch.setattr(yunlin_ebus, "_fetch_eta_at_stop", lambda stop: eta_data)

    snapshot = kiosk_departures.build_departure_snapshot(
        "雲林科技大學",
        go_back=2,
        updated_at=_updated_at(),
    )

    assert snapshot.stop_name == "雲林科技大學"
    assert snapshot.direction_filter == 2
    assert snapshot.updated_at == _updated_at()
    assert snapshot.summary.available_count == 4
    assert snapshot.summary.not_departed_count == 1
    assert snapshot.summary.last_departed_count == 1
    assert [route.route for route in snapshot.routes] == [
        "201",
        "301",
        "302",
        "7101",
        "7000B",
        "101",
    ]
    assert [route.decision for route in snapshot.routes] == [
        "arriving_soon",
        "can_wait",
        "long_wait",
        "scheduled",
        "not_departed",
        "last_departed",
    ]
    assert snapshot.routes[0].status_text == "即將到站"
    assert snapshot.routes[1].decision_text == "可以等"
    assert snapshot.routes[2].decision_text == "等待較久"
    assert snapshot.routes[3].scheduled_time == "21:35"
    assert snapshot.routes[4].section == "not_departed"
    assert snapshot.routes[5].section == "last_departed"


def test_build_departure_snapshot_applies_direction_filter(monkeypatch):
    route_info = {
        "201": {
            "id": 65036,
            "go_dest": "雲林科技大學",
            "back_dest": "高鐵雲林站",
        },
    }
    eta_data = [
        {"xno": 65036, "GoBack": 1, "Value": 6, "ComeTime": ""},
        {"xno": 65036, "GoBack": 2, "Value": 8, "ComeTime": ""},
    ]

    monkeypatch.setattr(yunlin_ebus, "_load_route_info", lambda stop: route_info)
    monkeypatch.setattr(yunlin_ebus, "_fetch_eta_at_stop", lambda stop: eta_data)

    snapshot = kiosk_departures.build_departure_snapshot(
        "雲林科技大學",
        go_back=1,
        updated_at=_updated_at(),
    )

    assert len(snapshot.routes) == 1
    assert snapshot.routes[0].go_back == 1
    assert snapshot.routes[0].direction == "往雲林科技大學"


def test_build_departure_snapshot_marks_unexpected_values_unknown(monkeypatch):
    route_info = {
        "201": {
            "id": 65036,
            "go_dest": "雲林科技大學",
            "back_dest": "高鐵雲林站",
        },
    }
    eta_data = [{"xno": 65036, "GoBack": 2, "Value": -1, "ComeTime": ""}]

    monkeypatch.setattr(yunlin_ebus, "_load_route_info", lambda stop: route_info)
    monkeypatch.setattr(yunlin_ebus, "_fetch_eta_at_stop", lambda stop: eta_data)

    snapshot = kiosk_departures.build_departure_snapshot(
        "雲林科技大學",
        go_back=2,
        updated_at=_updated_at(),
    )

    assert snapshot.summary.unknown_count == 1
    assert snapshot.routes[0].section == "unknown"
    assert snapshot.routes[0].decision == "unknown"
    assert snapshot.routes[0].decision_text == "資料異常"


def test_build_departure_snapshot_wraps_provider_errors(monkeypatch):
    def unavailable(stop_name):
        raise RuntimeError("upstream failed")

    monkeypatch.setattr(yunlin_ebus, "_fetch_eta_at_stop", unavailable)

    with pytest.raises(kiosk_departures.DepartureSnapshotUnavailable) as error:
        kiosk_departures.build_departure_snapshot("雲林科技大學")

    assert "雲林公車查詢失敗" in str(error.value)


def test_build_route_detail_returns_structured_stop_order(monkeypatch):
    route_info = {
        "201": {
            "id": 65036,
            "go_dest": "雲林科技大學",
            "back_dest": "高鐵雲林站",
        },
    }
    estimate_data = [
        {"StopName": "高鐵雲林站", "SeqNo": 3, "GoBack": 2, "Value": None},
        {"StopName": "雲林科技大學", "SeqNo": 1, "GoBack": 2, "Value": 0},
        {"StopName": "大學路口", "SeqNo": 2, "GoBack": 2, "Value": 4},
        {"StopName": "雲林科技大學", "SeqNo": 4, "GoBack": 1, "Value": -3},
    ]

    monkeypatch.setattr(yunlin_ebus, "_load_route_info", lambda stop: route_info)
    monkeypatch.setattr(
        yunlin_ebus,
        "_fetch_route_estimate",
        lambda route_id: estimate_data,
    )

    detail = kiosk_departures.build_route_detail(
        "201",
        "雲林科技大學",
        go_back=2,
    )

    assert detail.route == "201"
    assert detail.route_id == 65036
    assert detail.stop_name == "雲林科技大學"
    assert detail.direction_filter == 2
    assert len(detail.directions) == 1
    direction = detail.directions[0]
    assert direction.go_back == 2
    assert direction.label == "往高鐵雲林站"
    assert [stop.name for stop in direction.stops] == [
        "雲林科技大學",
        "大學路口",
        "高鐵雲林站",
    ]
    assert direction.stops[0].is_current_stop is True
    assert direction.stops[0].status_text == "即將到站"
    assert direction.stops[1].minutes == 4
    assert direction.stops[2].status_text == "未發車"


def test_build_route_detail_raises_not_found_for_non_kiosk_route(monkeypatch):
    monkeypatch.setattr(yunlin_ebus, "_load_route_info", lambda stop: {})

    with pytest.raises(kiosk_departures.RouteDetailNotFound) as error:
        kiosk_departures.build_route_detail("999", "雲林科技大學")

    assert "找不到停靠路線 999" in str(error.value)
