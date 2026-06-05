import asyncio

from services.kiosk_config import KioskConfig
from tools import kiosk_bus


def test_get_route_stops_uses_kiosk_config_stop_name(monkeypatch):
    calls = []

    async def fake_render(route, stop_name):
        calls.append((route, stop_name))
        return "ok"

    monkeypatch.setattr(
        kiosk_bus,
        "get_kiosk_config",
        lambda: KioskConfig(stop_name="雲林科技大學", direction="回程"),
    )
    monkeypatch.setattr(kiosk_bus.departures, "render_route_stops", fake_render)

    assert asyncio.run(kiosk_bus.get_route_stops("201")) == "ok"
    assert calls == [("201", "雲林科技大學")]


def test_get_stop_arrival_statuses_here_passes_direction_filter(monkeypatch):
    calls = []

    async def fake_render(stop_name, go_back):
        calls.append((stop_name, go_back))
        return "ok"

    monkeypatch.setattr(
        kiosk_bus,
        "get_kiosk_config",
        lambda: KioskConfig(stop_name="雲林科技大學", direction="回程"),
    )
    monkeypatch.setattr(kiosk_bus.departures, "render_stop_arrival_statuses", fake_render)

    assert asyncio.run(kiosk_bus.get_stop_arrival_statuses_here()) == "ok"
    assert calls == [("雲林科技大學", 2)]
