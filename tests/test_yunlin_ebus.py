from tools import kiosk_bus, yunlin_ebus


class Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def test_route_cache_uses_stop_lookup(monkeypatch):
    payload = [
        {
            "xno": "65036",
            "name": "201",
            "goback": "1",
            "departure": "高鐵雲林站",
            "destination": "雲林科技大學",
        },
        {
            "xno": "65036",
            "name": "201",
            "goback": "2",
            "departure": "高鐵雲林站",
            "destination": "雲林科技大學",
        },
    ]
    calls = []

    def fake_get(url, params=None, timeout=None):
        calls.append((url, params, timeout))
        return Response(payload)

    yunlin_ebus._route_info_by_stop.clear()
    monkeypatch.setattr(yunlin_ebus.requests, "get", fake_get)

    assert yunlin_ebus._get_route_id("201", "雲林科技大學") == 65036
    assert yunlin_ebus._direction_label("201", "雲林科技大學", 1) == "往雲林科技大學"
    assert yunlin_ebus._direction_label("201", "雲林科技大學", 2) == "往高鐵雲林站"
    assert calls == [
        (
            "https://ebus.yunlin.gov.tw/api/stop/route",
            {"stop_name": "雲林科技大學"},
            10,
        )
    ]


def test_route_cache_rejects_ambiguous_names(monkeypatch):
    payload = [
        {"xno": "1", "name": "201"},
        {"xno": "2", "name": "201"},
    ]

    yunlin_ebus._route_info_by_stop.clear()
    monkeypatch.setattr(
        yunlin_ebus.requests,
        "get",
        lambda *args, **kwargs: Response(payload),
    )

    assert yunlin_ebus._get_route_id("201", "測試站") is None


def test_route_stops_uses_kiosk_stop(monkeypatch):
    calls = []

    def fake_route_stops(route, stop_name):
        calls.append((route, stop_name))
        return "ok"

    monkeypatch.setenv("KIOSK_STOP", "雲科")
    monkeypatch.setattr(kiosk_bus.yunlin_ebus, "get_route_stops", fake_route_stops)

    assert kiosk_bus.get_route_stops("201") == "ok"
    assert calls == [("201", "雲林科技大學")]


def test_arrival_status_matches_ebus_value_codes():
    assert yunlin_ebus._arrival_status({"Value": -3}) == "末班駛離"
    assert yunlin_ebus._arrival_status({"Value": 2}) == "即將到站"
    assert yunlin_ebus._arrival_status({"Value": 12}) == "約 12 分鐘後"
    assert (
        yunlin_ebus._arrival_status({"Value": None, "ComeTime": "21:00"})
        == "預定 21:00"
    )
    assert yunlin_ebus._arrival_status({"Value": None, "ComeTime": ""}) == "未發車"


def test_stop_arrival_statuses_group_stop_eta(monkeypatch):
    route_info = {
        "201": {
            "id": 65036,
            "go_dest": "雲林科技大學",
            "back_dest": "高鐵雲林站",
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
        {"xno": 65036, "GoBack": 2, "Value": None, "ComeTime": "21:35"},
        {"xno": 65352, "GoBack": 2, "Value": None, "ComeTime": ""},
        {"xno": 15121, "GoBack": 2, "Value": -3, "ComeTime": ""},
    ]

    monkeypatch.setattr(yunlin_ebus, "_load_route_info", lambda stop: route_info)
    monkeypatch.setattr(yunlin_ebus, "_fetch_eta_at_stop", lambda stop: eta_data)

    assert yunlin_ebus.get_stop_arrival_statuses("雲林科技大學", go_back=2) == (
        "「雲林科技大學」目前到站狀態：\n"
        "尚有到站資訊：\n"
        "201 往高鐵雲林站：預定 21:35\n"
        "未發車：\n"
        "7000B 往梅山站：未發車\n"
        "末班駛離：\n"
        "101 往斗六棒球場：末班駛離"
    )


def test_stop_arrival_statuses_here_uses_kiosk_direction(monkeypatch):
    calls = []

    def fake_statuses(stop_name, go_back):
        calls.append((stop_name, go_back))
        return "ok"

    monkeypatch.setenv("KIOSK_STOP", "雲科")
    monkeypatch.setenv("KIOSK_DIRECTION", "回程")
    monkeypatch.setattr(
        kiosk_bus.yunlin_ebus, "get_stop_arrival_statuses", fake_statuses
    )

    assert kiosk_bus.get_stop_arrival_statuses_here() == "ok"
    assert calls == [("雲林科技大學", 2)]
