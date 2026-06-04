import asyncio
from datetime import datetime

import pytest

from providers.bus import BusProvider
from services import departures


def _updated_at() -> datetime:
    return datetime.fromisoformat("2026-05-24T12:00:00+08:00")


class FakeBusProvider(BusProvider):
    """In-memory BusProvider used to drive `services.departures` from tests.

    Lets each case spell out only the upstream data it cares about and
    raise on anything unexpected — surfaces accidental Protocol drift
    instead of silently returning empty payloads.
    """

    def __init__(
        self,
        *,
        route_info: dict[str, dict] | None = None,
        eta_at_stop: list[dict] | None = None,
        route_estimate: list[dict] | None = None,
        routes_at_stop: list[dict] | None = None,
        eta_error: Exception | None = None,
        route_estimate_error: Exception | None = None,
    ) -> None:
        self._route_info = route_info or {}
        self._eta_at_stop = eta_at_stop or []
        self._route_estimate = route_estimate or []
        self._routes_at_stop = routes_at_stop or []
        self._eta_error = eta_error
        self._route_estimate_error = route_estimate_error

    async def fetch_routes_at_stop(self, stop_name: str) -> list[dict]:
        return self._routes_at_stop

    async def fetch_eta_at_stop(self, stop_name: str) -> list[dict]:
        if self._eta_error is not None:
            raise self._eta_error
        return self._eta_at_stop

    async def fetch_route_estimate(self, route_id: int) -> list[dict]:
        if self._route_estimate_error is not None:
            raise self._route_estimate_error
        return self._route_estimate

    async def load_route_info(self, stop_name: str) -> dict[str, dict]:
        return self._route_info


@pytest.fixture
def use_provider(monkeypatch):
    """Swap `services.departures._provider` for the duration of a test."""

    def _install(provider: BusProvider) -> BusProvider:
        monkeypatch.setattr(departures, "_provider", provider)
        return provider

    return _install


def test_build_departure_snapshot_classifies_and_sorts_routes(use_provider):
    use_provider(
        FakeBusProvider(
            route_info={
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
            },
            eta_at_stop=[
                {"xno": 302, "GoBack": 2, "Value": 25, "ComeTime": ""},
                {"xno": 15121, "GoBack": 2, "Value": -3, "ComeTime": ""},
                {"xno": 65036, "GoBack": 2, "Value": 2, "ComeTime": ""},
                {"xno": 65352, "GoBack": 2, "Value": None, "ComeTime": ""},
                {"xno": 301, "GoBack": 2, "Value": 12, "ComeTime": ""},
                {"xno": 7101, "GoBack": 2, "Value": None, "ComeTime": "21:35"},
            ],
        )
    )

    snapshot = asyncio.run(departures.build_departure_snapshot(
        "雲林科技大學",
        go_back=2,
        updated_at=_updated_at(),
    ))

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


def test_build_departure_snapshot_applies_direction_filter(use_provider):
    use_provider(
        FakeBusProvider(
            route_info={
                "201": {
                    "id": 65036,
                    "go_dest": "雲林科技大學",
                    "back_dest": "高鐵雲林站",
                },
            },
            eta_at_stop=[
                {"xno": 65036, "GoBack": 1, "Value": 6, "ComeTime": ""},
                {"xno": 65036, "GoBack": 2, "Value": 8, "ComeTime": ""},
            ],
        )
    )

    snapshot = asyncio.run(departures.build_departure_snapshot(
        "雲林科技大學",
        go_back=1,
        updated_at=_updated_at(),
    ))

    assert len(snapshot.routes) == 1
    assert snapshot.routes[0].go_back == 1
    assert snapshot.routes[0].direction == "往雲林科技大學"


def test_build_departure_snapshot_marks_unexpected_values_unknown(use_provider):
    use_provider(
        FakeBusProvider(
            route_info={
                "201": {
                    "id": 65036,
                    "go_dest": "雲林科技大學",
                    "back_dest": "高鐵雲林站",
                },
            },
            eta_at_stop=[{"xno": 65036, "GoBack": 2, "Value": -1, "ComeTime": ""}],
        )
    )

    snapshot = asyncio.run(departures.build_departure_snapshot(
        "雲林科技大學",
        go_back=2,
        updated_at=_updated_at(),
    ))

    assert snapshot.summary.unknown_count == 1
    assert snapshot.routes[0].section == "unknown"
    assert snapshot.routes[0].decision == "unknown"
    assert snapshot.routes[0].decision_text == "資料異常"


def test_build_departure_snapshot_wraps_provider_errors(use_provider):
    use_provider(FakeBusProvider(eta_error=RuntimeError("upstream failed")))

    with pytest.raises(departures.DepartureSnapshotUnavailable) as error:
        asyncio.run(departures.build_departure_snapshot("雲林科技大學"))

    assert "公車資訊暫時無法取得" in str(error.value)


def test_build_route_detail_returns_structured_stop_order(use_provider):
    use_provider(
        FakeBusProvider(
            route_info={
                "201": {
                    "id": 65036,
                    "go_dest": "雲林科技大學",
                    "back_dest": "高鐵雲林站",
                },
            },
            route_estimate=[
                {"StopName": "高鐵雲林站", "SeqNo": 3, "GoBack": 2, "Value": None},
                {"StopName": "雲林科技大學", "SeqNo": 1, "GoBack": 2, "Value": 0},
                {"StopName": "大學路口", "SeqNo": 2, "GoBack": 2, "Value": 4},
                {"StopName": "雲林科技大學", "SeqNo": 4, "GoBack": 1, "Value": -3},
            ],
        )
    )

    detail = asyncio.run(departures.build_route_detail(
        "201",
        "雲林科技大學",
        go_back=2,
    ))

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


def test_build_route_detail_raises_not_found_for_non_kiosk_route(use_provider):
    use_provider(FakeBusProvider(route_info={}))

    with pytest.raises(departures.RouteDetailNotFound) as error:
        asyncio.run(departures.build_route_detail("999", "雲林科技大學"))

    assert "找不到停靠路線 999" in str(error.value)


# ── string renderers ──────────────────────────────────────────────────────────


def test_render_stop_arrival_statuses_groups_by_section(use_provider):
    use_provider(
        FakeBusProvider(
            route_info={
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
            },
            eta_at_stop=[
                {"xno": 65036, "GoBack": 2, "Value": None, "ComeTime": "21:35"},
                {"xno": 65352, "GoBack": 2, "Value": None, "ComeTime": ""},
                {"xno": 15121, "GoBack": 2, "Value": -3, "ComeTime": ""},
            ],
        )
    )

    statuses = asyncio.run(
        departures.render_stop_arrival_statuses("雲林科技大學", go_back=2)
    )
    assert statuses == (
        "雲林科技大學 目前到站狀態：\n"
        "尚有到站資訊：\n"
        "201 往高鐵雲林站：預定 21:35\n"
        "未發車：\n"
        "7000B 往梅山站：未發車\n"
        "末班駛離：\n"
        "101 往斗六棒球場：末班駛離"
    )


def test_render_arrivals_uses_classify(use_provider):
    use_provider(
        FakeBusProvider(
            route_info={
                "201": {
                    "id": 65036,
                    "go_dest": "雲林科技大學",
                    "back_dest": "高鐵雲林站",
                },
            },
            route_estimate=[
                {"StopName": "雲林科技大學", "GoBack": 1, "Value": 2, "ComeTime": ""},
                {"StopName": "雲林科技大學", "GoBack": 2, "Value": 12, "ComeTime": ""},
                {"StopName": "其他站", "GoBack": 1, "Value": 0, "ComeTime": ""},
            ],
        )
    )

    assert asyncio.run(departures.render_arrivals("201", "雲林科技大學")) == (
        "往雲林科技大學：即將到站\n"
        "往高鐵雲林站：約十二分鐘後"
    )


def test_render_route_stops_lists_both_directions(use_provider):
    use_provider(
        FakeBusProvider(
            route_info={
                "201": {
                    "id": 65036,
                    "go_dest": "雲林科技大學",
                    "back_dest": "高鐵雲林站",
                },
            },
            route_estimate=[
                {"StopName": "高鐵雲林站", "SeqNo": 1, "GoBack": 1, "Value": None},
                {"StopName": "雲林科技大學", "SeqNo": 2, "GoBack": 1, "Value": 0},
                {"StopName": "雲林科技大學", "SeqNo": 1, "GoBack": 2, "Value": 0},
                {"StopName": "高鐵雲林站", "SeqNo": 2, "GoBack": 2, "Value": 4},
            ],
        )
    )

    assert asyncio.run(departures.render_route_stops("201", "雲林科技大學")) == (
        "往雲林科技大學：高鐵雲林站→雲林科技大學\n"
        "往高鐵雲林站：雲林科技大學→高鐵雲林站"
    )


def test_render_routes_at_stop_lists_unique_routes(use_provider):
    use_provider(
        FakeBusProvider(
            routes_at_stop=[
                {"name": "201", "ddesc": "高鐵雲林站－雲林科技大學"},
                {"name": "201", "ddesc": "高鐵雲林站－雲林科技大學"},
                {"name": "7126", "ddesc": "斗六－雲林科技大學"},
            ],
        )
    )

    assert asyncio.run(departures.render_routes_at_stop("雲林科技大學")) == (
        "雲林科技大學 停靠路線：\n"
        "201\n"
        "7126"
    )


def test_render_stop_on_route_found(use_provider):
    use_provider(
        FakeBusProvider(
            route_info={"201": {"id": 1, "go_dest": "高鐵雲林站", "back_dest": "雲林科技大學"}},
            route_estimate=[
                {"GoBack": 1, "SeqNo": 1, "StopName": "雲林科技大學", "Value": 10},
                {"GoBack": 1, "SeqNo": 2, "StopName": "斗六火車站", "Value": 20},
            ],
        )
    )
    result = asyncio.run(departures.render_stop_on_route("201", "斗六火車站", "雲林科技大學"))
    assert result.startswith("有")
    assert "201" in result
    assert "斗六火車站" in result


def test_render_stop_on_route_not_found(use_provider):
    use_provider(
        FakeBusProvider(
            route_info={"201": {"id": 1, "go_dest": "高鐵雲林站", "back_dest": "雲林科技大學"}},
            route_estimate=[
                {"GoBack": 1, "SeqNo": 1, "StopName": "雲林科技大學", "Value": 10},
            ],
        )
    )
    result = asyncio.run(departures.render_stop_on_route("201", "台北101", "雲林科技大學"))
    assert result.startswith("沒有")


def test_render_routes_to_destination_found(use_provider):
    use_provider(
        FakeBusProvider(
            route_info={"201": {"id": 1, "go_dest": "高鐵雲林站", "back_dest": "雲林科技大學"}},
            route_estimate=[
                {"GoBack": 1, "SeqNo": 1, "StopName": "雲林科技大學", "Value": 0},
                {"GoBack": 1, "SeqNo": 2, "StopName": "斗六火車站", "Value": 10},
                {"GoBack": 1, "SeqNo": 3, "StopName": "高鐵雲林站", "Value": 20},
            ],
        )
    )
    result = asyncio.run(departures.render_routes_to_destination("斗六", "雲林科技大學"))
    assert "201" in result
    assert result != "本站沒有直達斗六的路線"


def test_render_routes_to_destination_not_found(use_provider):
    use_provider(
        FakeBusProvider(
            route_info={"201": {"id": 1, "go_dest": "高鐵雲林站", "back_dest": "雲林科技大學"}},
            route_estimate=[
                {"GoBack": 1, "SeqNo": 1, "StopName": "雲林科技大學", "Value": 10},
            ],
        )
    )
    result = asyncio.run(departures.render_routes_to_destination("台北101", "雲林科技大學"))
    assert "沒有" in result


# ── geo-awareness ────────────────────────────────────────────────────────────
#
# Both `render_routes_to_destination` and `render_stop_on_route` must only
# count a direction as 有 when the destination is at or after the kiosk's
# position in the stop sequence. A bus passing the destination *before*
# reaching the kiosk cannot take a passenger boarding at the kiosk there.


def test_render_routes_to_destination_skips_upstream_destination(use_provider):
    """If destination's seq < kiosk's seq, that direction does not count."""
    use_provider(
        FakeBusProvider(
            route_info={
                "201": {"id": 1, "go_dest": "東邊終點", "back_dest": "西邊終點"},
            },
            route_estimate=[
                # GoBack=1 (往東): 虎尾(1) → 雲科大(2) → 斗六(3)
                # From kiosk 雲科大, 虎尾 is upstream — bus already passed it.
                {"GoBack": 1, "SeqNo": 1, "StopName": "虎尾", "Value": 5},
                {"GoBack": 1, "SeqNo": 2, "StopName": "雲林科技大學", "Value": 10},
                {"GoBack": 1, "SeqNo": 3, "StopName": "斗六", "Value": 15},
            ],
        )
    )
    # 斗六 is downstream → hit
    result = asyncio.run(
        departures.render_routes_to_destination("斗六", "雲林科技大學")
    )
    assert "201" in result

    # 虎尾 is upstream → no hit
    result = asyncio.run(
        departures.render_routes_to_destination("虎尾", "雲林科技大學")
    )
    assert result == "本站沒有直達虎尾的路線"


def test_render_routes_to_destination_skips_directions_without_kiosk(use_provider):
    """If kiosk doesn't appear in a direction, skip it entirely."""
    use_provider(
        FakeBusProvider(
            route_info={
                "201": {"id": 1, "go_dest": "東邊終點", "back_dest": "西邊終點"},
            },
            route_estimate=[
                # GoBack=1 has kiosk + destination downstream
                {"GoBack": 1, "SeqNo": 1, "StopName": "雲林科技大學", "Value": 0},
                {"GoBack": 1, "SeqNo": 2, "StopName": "斗六", "Value": 10},
                # GoBack=2 doesn't include kiosk — should be ignored
                {"GoBack": 2, "SeqNo": 1, "StopName": "斗六", "Value": 5},
                {"GoBack": 2, "SeqNo": 2, "StopName": "別的站", "Value": 10},
            ],
        )
    )
    result = asyncio.run(
        departures.render_routes_to_destination("斗六", "雲林科技大學")
    )
    # Only one direction matched → direction label preserved
    assert "201" in result
    assert "往東邊終點" in result


def test_render_routes_to_destination_collapses_when_both_downstream(use_provider):
    """Same route, destination downstream in both directions → no direction label."""
    use_provider(
        FakeBusProvider(
            route_info={
                "7120": {"id": 1, "go_dest": "高鐵雲林站", "back_dest": "斗六火車站"},
            },
            route_estimate=[
                # GoBack=1: kiosk → 虎尾科大 → 高鐵
                {"GoBack": 1, "SeqNo": 1, "StopName": "雲林科技大學", "Value": 0},
                {"GoBack": 1, "SeqNo": 2, "StopName": "虎尾科大", "Value": 10},
                {"GoBack": 1, "SeqNo": 3, "StopName": "高鐵雲林站", "Value": 20},
                # GoBack=2: kiosk → 虎尾科大 → 斗六 (loops through 虎尾科大 both ways)
                {"GoBack": 2, "SeqNo": 1, "StopName": "雲林科技大學", "Value": 0},
                {"GoBack": 2, "SeqNo": 2, "StopName": "虎尾科大", "Value": 12},
                {"GoBack": 2, "SeqNo": 3, "StopName": "斗六火車站", "Value": 25},
            ],
        )
    )
    result = asyncio.run(
        departures.render_routes_to_destination("虎尾科大", "雲林科技大學")
    )
    # Both directions reach 虎尾科大 → collapse to bare route number with 搭 prefix
    assert result == "搭 7120"


def test_render_stop_on_route_rejects_upstream_destination(use_provider):
    """`check_stop_on_route` is geo-aware too: upstream destination → 沒有."""
    use_provider(
        FakeBusProvider(
            route_info={
                "201": {"id": 1, "go_dest": "東邊終點", "back_dest": "西邊終點"},
            },
            route_estimate=[
                # 虎尾 is at seq 1, kiosk at seq 2 — upstream from kiosk.
                {"GoBack": 1, "SeqNo": 1, "StopName": "虎尾", "Value": 5},
                {"GoBack": 1, "SeqNo": 2, "StopName": "雲林科技大學", "Value": 10},
            ],
        )
    )
    result = asyncio.run(departures.render_stop_on_route("201", "虎尾", "雲林科技大學"))
    assert result.startswith("沒有")


def test_render_stop_on_route_allows_kiosk_itself(use_provider):
    """Asking '有沒有停 <kiosk>' at the kiosk should answer 有 (trivially)."""
    use_provider(
        FakeBusProvider(
            route_info={
                "201": {"id": 1, "go_dest": "東邊終點", "back_dest": "西邊終點"},
            },
            route_estimate=[
                {"GoBack": 1, "SeqNo": 1, "StopName": "雲林科技大學", "Value": 0},
                {"GoBack": 1, "SeqNo": 2, "StopName": "斗六", "Value": 10},
            ],
        )
    )
    result = asyncio.run(departures.render_stop_on_route(
        "201", "雲林科技大學", "雲林科技大學"
    ))
    assert result.startswith("有")
