import asyncio

from services.kiosk_config import KioskConfig
from tools import kiosk_bus


def test_prefetch_route_arrival_context_queries_route_number(monkeypatch):
    async def fake_arrivals(route):
        return f"{route} 到站"

    monkeypatch.setattr(kiosk_bus, "get_arrivals_here", fake_arrivals)

    assert asyncio.run(kiosk_bus.prefetch_route_arrival_context("我想搭 201")) == (
        "\n\n[預取到站資訊，僅供參考；仍須依決策規則判斷是否直接回應]\n"
        "路線 201 到本站的資訊：\n201 到站"
    )


def test_prefetch_route_arrival_context_returns_rule1_hint_for_route_only(monkeypatch):
    async def fake_arrivals(route):
        return "不該查"

    monkeypatch.setattr(kiosk_bus, "get_arrivals_here", fake_arrivals)

    result = asyncio.run(kiosk_bus.prefetch_route_arrival_context("201"))
    assert "規則1" in result
    assert "禁止呼叫" in result


def test_prefetch_route_arrival_context_ignores_time_amount(monkeypatch):
    async def fake_arrivals(route):
        return "不該查"

    monkeypatch.setattr(kiosk_bus, "get_arrivals_here", fake_arrivals)

    assert asyncio.run(kiosk_bus.prefetch_route_arrival_context("還要 30 分鐘嗎")) == ""


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


def test_prefetch_route_arrival_context_fullwidth_route_triggers_rule1(monkeypatch):
    async def fake_arrivals(route):
        return "不該查"

    monkeypatch.setattr(kiosk_bus, "get_arrivals_here", fake_arrivals)

    result = asyncio.run(kiosk_bus.prefetch_route_arrival_context("Ｙ01"))
    assert "規則1" in result
    assert "禁止呼叫" in result


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
    monkeypatch.setattr(
        kiosk_bus.departures, "render_stop_arrival_statuses", fake_render
    )

    assert asyncio.run(kiosk_bus.get_stop_arrival_statuses_here()) == "ok"
    assert calls == [("雲林科技大學", 2)]
