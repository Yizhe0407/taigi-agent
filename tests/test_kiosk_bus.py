from tools import kiosk_bus


def test_prefetch_route_arrival_context_queries_route_number(monkeypatch):
    monkeypatch.setattr(kiosk_bus, "get_arrivals_here", lambda route: f"{route} 到站")

    assert kiosk_bus.prefetch_route_arrival_context("我想搭 201") == (
        "\n\n[工具查詢結果，必須直接使用，禁止用訓練資料替代]\n"
        "路線 201 到本站的資訊：\n201 到站"
    )


def test_prefetch_route_arrival_context_ignores_time_amount(monkeypatch):
    monkeypatch.setattr(kiosk_bus, "get_arrivals_here", lambda route: "不該查")

    assert kiosk_bus.prefetch_route_arrival_context("還要 30 分鐘嗎") == ""
