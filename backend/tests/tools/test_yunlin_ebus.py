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

    def fake_render(route, stop_name):
        calls.append((route, stop_name))
        return "ok"

    monkeypatch.setenv("KIOSK_STOP", "雲科")
    monkeypatch.setattr(kiosk_bus.departures, "render_route_stops", fake_render)

    assert kiosk_bus.get_route_stops("201") == "ok"
    assert calls == [("201", "雲林科技大學")]


def test_stop_arrival_statuses_here_uses_kiosk_direction(monkeypatch):
    calls = []

    def fake_render(stop_name, go_back):
        calls.append((stop_name, go_back))
        return "ok"

    monkeypatch.setenv("KIOSK_STOP", "雲科")
    monkeypatch.setenv("KIOSK_DIRECTION", "回程")
    monkeypatch.setattr(
        kiosk_bus.departures, "render_stop_arrival_statuses", fake_render
    )

    assert kiosk_bus.get_stop_arrival_statuses_here() == "ok"
    assert calls == [("雲林科技大學", 2)]
