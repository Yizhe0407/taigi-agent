import asyncio
from datetime import datetime

import pytest

from providers.bus import BusProvider
from services import departures
from services.departures import provider as _departures_provider
from services.departures.normalize import _is_terminal_direction


def _updated_at() -> datetime:
    return datetime.fromisoformat("2026-05-24T12:00:00+08:00")


class FakeBusProvider(BusProvider):
    """In-memory BusProvider used to drive `services.departures` from tests.

    TDX field schema:
      fetch_eta_at_stop rows:    sub_route_name, direction (0/1), stop_status (0-4), estimate_seconds
      fetch_route_estimate rows: stop_name, stop_sequence, direction (0/1), stop_status, estimate_seconds
      fetch_routes_at_stop rows: sub_route_name, direction
      load_route_info values:    {id: str, go_dest: str, back_dest: str}
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

    async def fetch_route_estimate(self, sub_route_name: str) -> list[dict]:
        if self._route_estimate_error is not None:
            raise self._route_estimate_error
        return self._route_estimate

    async def load_route_info(self, stop_name: str) -> dict[str, dict]:
        return self._route_info


@pytest.fixture
def use_provider(monkeypatch):
    """Swap `services.departures.provider._provider` for the duration of a test."""

    def _install(provider: BusProvider) -> BusProvider:
        monkeypatch.setattr(_departures_provider, "_provider", provider)
        return provider

    return _install


def test_build_departure_snapshot_classifies_and_sorts_routes(use_provider):
    use_provider(
        FakeBusProvider(
            route_info={
                "201": {"id": "201", "go_dest": "雲林科技大學", "back_dest": "高鐵雲林站"},
                "301": {"id": "301", "go_dest": "雲林科技大學", "back_dest": "斗六"},
                "302": {"id": "302", "go_dest": "雲林科技大學", "back_dest": "虎尾"},
                "7101": {"id": "7101", "go_dest": "雲林科技大學", "back_dest": "麥寮"},
                "7000D": {"id": "7000D", "go_dest": "台北站", "back_dest": "梅山站"},
                "101": {"id": "101", "go_dest": "受天宮", "back_dest": "斗六棒球場"},
            },
            eta_at_stop=[
                # direction=1 = 回程
                {"sub_route_name": "302", "direction": 1, "stop_status": 0, "estimate_seconds": 1500},
                {"sub_route_name": "101", "direction": 1, "stop_status": 3, "estimate_seconds": None},
                {"sub_route_name": "201", "direction": 1, "stop_status": 0, "estimate_seconds": 120},
                {"sub_route_name": "7000D", "direction": 1, "stop_status": 1, "estimate_seconds": None},
                {"sub_route_name": "301", "direction": 1, "stop_status": 0, "estimate_seconds": 720},
                {"sub_route_name": "7101", "direction": 1, "stop_status": 4, "estimate_seconds": None},
            ],
        )
    )

    snapshot = asyncio.run(
        departures.build_departure_snapshot(
            "雲林科技大學",
            go_back=1,  # 回程 = direction 1
            updated_at=_updated_at(),
        )
    )

    assert snapshot.stop_name == "雲林科技大學"
    assert snapshot.direction_filter == 1
    assert snapshot.updated_at == _updated_at()
    assert snapshot.summary.available_count == 3
    assert snapshot.summary.not_departed_count == 1  # only stop_status=1; status=4 → unknown
    assert snapshot.summary.last_departed_count == 1
    assert snapshot.summary.unknown_count == 1  # stop_status=4 (今日未營運)
    assert [route.route for route in snapshot.routes] == [
        "201",
        "301",
        "302",
        "7000D",
        "101",
        "7101",
    ]
    assert [route.decision for route in snapshot.routes] == [
        "arriving_soon",
        "can_wait",
        "long_wait",
        "not_departed",
        "last_departed",
        "unknown",
    ]
    assert snapshot.routes[0].status_text == "即將到站"
    assert snapshot.routes[1].decision_text == "可以等"
    assert snapshot.routes[2].decision_text == "等待較久"
    assert snapshot.routes[3].section == "not_departed"
    assert snapshot.routes[4].section == "last_departed"
    assert snapshot.routes[5].status_text == "今日未營運"
    assert snapshot.routes[5].section == "unknown"


def test_build_departure_snapshot_applies_direction_filter(use_provider):
    use_provider(
        FakeBusProvider(
            route_info={
                "201": {"id": "201", "go_dest": "雲林科技大學", "back_dest": "高鐵雲林站"},
            },
            eta_at_stop=[
                {"sub_route_name": "201", "direction": 0, "stop_status": 0, "estimate_seconds": 360},
                {"sub_route_name": "201", "direction": 1, "stop_status": 0, "estimate_seconds": 480},
            ],
        )
    )

    snapshot = asyncio.run(
        departures.build_departure_snapshot(
            "雲林科技大學",
            go_back=0,  # 去程 = direction 0
            updated_at=_updated_at(),
        )
    )

    assert len(snapshot.routes) == 1
    assert snapshot.routes[0].go_back == 0
    assert snapshot.routes[0].direction == "往雲林科技大學"


def test_build_departure_snapshot_marks_unexpected_values_unknown(use_provider):
    use_provider(
        FakeBusProvider(
            route_info={
                "201": {"id": "201", "go_dest": "雲林科技大學", "back_dest": "高鐵雲林站"},
            },
            # stop_status=0 but no estimate_seconds → data inconsistency → UNKNOWN
            eta_at_stop=[{"sub_route_name": "201", "direction": 1, "stop_status": 0, "estimate_seconds": None}],
        )
    )

    snapshot = asyncio.run(
        departures.build_departure_snapshot(
            "雲林科技大學",
            go_back=1,
            updated_at=_updated_at(),
        )
    )

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
                "201": {"id": "201", "go_dest": "雲林科技大學", "back_dest": "高鐵雲林站"},
            },
            route_estimate=[
                {"stop_name": "高鐵雲林站", "stop_sequence": 3, "direction": 1, "stop_status": 1, "estimate_seconds": None},
                {"stop_name": "雲林科技大學", "stop_sequence": 1, "direction": 1, "stop_status": 0, "estimate_seconds": 0},
                {"stop_name": "大學路口", "stop_sequence": 2, "direction": 1, "stop_status": 0, "estimate_seconds": 240},
                {"stop_name": "雲林科技大學", "stop_sequence": 4, "direction": 0, "stop_status": 3, "estimate_seconds": None},
            ],
        )
    )

    detail = asyncio.run(
        departures.build_route_detail(
            "201",
            "雲林科技大學",
            go_back=1,  # 回程 = direction 1
        )
    )

    assert detail.route == "201"
    assert detail.route_id == "201"
    assert detail.stop_name == "雲林科技大學"
    assert detail.direction_filter == 1
    assert len(detail.directions) == 1
    direction = detail.directions[0]
    assert direction.go_back == 1
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
                "201": {"id": "201", "go_dest": "雲林科技大學", "back_dest": "高鐵雲林站"},
                "7000D": {"id": "7000D", "go_dest": "台北站", "back_dest": "梅山站"},
                "101": {"id": "101", "go_dest": "受天宮", "back_dest": "斗六棒球場"},
            },
            eta_at_stop=[
                {"sub_route_name": "201", "direction": 1, "stop_status": 0, "estimate_seconds": 720},
                {"sub_route_name": "7000D", "direction": 1, "stop_status": 1, "estimate_seconds": None},
                {"sub_route_name": "101", "direction": 1, "stop_status": 3, "estimate_seconds": None},
            ],
        )
    )

    statuses = asyncio.run(departures.render_stop_arrival_statuses("雲林科技大學", go_back=1))
    assert statuses == (
        "雲林科技大學 目前到站狀態：\n有車：\n201 往高鐵雲林站：約十二分鐘後\n尚未發車：\n7000D 往梅山站：未發車\n末班已過：\n101 往斗六棒球場：末班駛離"
    )


def test_render_arrivals_uses_classify(use_provider):
    use_provider(
        FakeBusProvider(
            route_info={
                "201": {"id": "201", "go_dest": "雲林科技大學", "back_dest": "高鐵雲林站"},
            },
            route_estimate=[
                {"stop_name": "雲林科技大學", "stop_sequence": 1, "direction": 0, "stop_status": 0, "estimate_seconds": 120},
                {"stop_name": "雲林科技大學", "stop_sequence": 1, "direction": 1, "stop_status": 0, "estimate_seconds": 720},
                {"stop_name": "其他站", "stop_sequence": 2, "direction": 0, "stop_status": 0, "estimate_seconds": 0},
            ],
        )
    )

    assert asyncio.run(departures.render_arrivals("201", "雲林科技大學")) == ("往雲林科技大學：即將到站\n往高鐵雲林站：約十二分鐘後來車")


def test_render_route_stops_lists_both_directions(use_provider):
    use_provider(
        FakeBusProvider(
            route_info={
                "201": {"id": "201", "go_dest": "雲林科技大學", "back_dest": "高鐵雲林站"},
            },
            route_estimate=[
                {"stop_name": "高鐵雲林站", "stop_sequence": 1, "direction": 0, "stop_status": 1, "estimate_seconds": None},
                {"stop_name": "雲林科技大學", "stop_sequence": 2, "direction": 0, "stop_status": 0, "estimate_seconds": 0},
                {"stop_name": "雲林科技大學", "stop_sequence": 1, "direction": 1, "stop_status": 0, "estimate_seconds": 0},
                {"stop_name": "高鐵雲林站", "stop_sequence": 2, "direction": 1, "stop_status": 0, "estimate_seconds": 240},
            ],
        )
    )

    assert asyncio.run(departures.render_route_stops("201", "雲林科技大學")) == (
        "往雲林科技大學：高鐵雲林站、雲林科技大學\n往高鐵雲林站：雲林科技大學、高鐵雲林站"
    )


def test_render_routes_at_stop_lists_unique_routes(use_provider):
    use_provider(
        FakeBusProvider(
            routes_at_stop=[
                {"sub_route_name": "201", "direction": 0},
                {"sub_route_name": "201", "direction": 1},
                {"sub_route_name": "7126", "direction": 0},
            ],
        )
    )

    assert asyncio.run(departures.render_routes_at_stop("雲林科技大學")) == ("雲林科技大學 停靠路線：\n201\n7126")


def test_render_stop_on_route_found(use_provider):
    use_provider(
        FakeBusProvider(
            route_info={"201": {"id": "201", "go_dest": "高鐵雲林站", "back_dest": "雲林科技大學"}},
            route_estimate=[
                {"direction": 0, "stop_sequence": 1, "stop_name": "雲林科技大學", "stop_status": 0, "estimate_seconds": 600},
                {"direction": 0, "stop_sequence": 2, "stop_name": "斗六火車站", "stop_status": 0, "estimate_seconds": 1200},
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
            route_info={"201": {"id": "201", "go_dest": "高鐵雲林站", "back_dest": "雲林科技大學"}},
            route_estimate=[
                {"direction": 0, "stop_sequence": 1, "stop_name": "雲林科技大學", "stop_status": 0, "estimate_seconds": 600},
            ],
        )
    )
    result = asyncio.run(departures.render_stop_on_route("201", "台北101", "雲林科技大學"))
    assert result.startswith("沒有")


# ── render_arrivals_to_destination ───────────────────────────────────────────


def test_render_arrivals_to_destination_returns_all_sorted(use_provider):
    """All routes with real-time data are returned sorted by ETA for LLM to reason over."""
    use_provider(
        FakeBusProvider(
            route_info={
                "201": {"id": "201", "go_dest": "斗六火車站", "back_dest": "雲林科技大學"},
                "301": {"id": "301", "go_dest": "斗六火車站", "back_dest": "雲林科技大學"},
            },
            route_estimate=[
                {"direction": 0, "stop_sequence": 1, "stop_name": "雲林科技大學", "stop_status": 0, "estimate_seconds": 480},
                {"direction": 0, "stop_sequence": 2, "stop_name": "斗六火車站", "stop_status": 0, "estimate_seconds": 1200},
            ],
        )
    )
    result = asyncio.run(departures.render_arrivals_to_destination("斗六火車站", "雲林科技大學"))
    assert "201" in result
    assert "301" in result
    assert "八分" in result


def test_render_arrivals_to_destination_no_routes(use_provider):
    """Returns 本站沒有直達 when destination not served."""
    use_provider(
        FakeBusProvider(
            route_info={"201": {"id": "201", "go_dest": "高鐵雲林站", "back_dest": "雲林科技大學"}},
            route_estimate=[
                {"direction": 0, "stop_sequence": 1, "stop_name": "雲林科技大學", "stop_status": 0, "estimate_seconds": 600},
            ],
        )
    )
    result = asyncio.run(departures.render_arrivals_to_destination("台北101", "雲林科技大學"))
    assert result == "本站沒有直達台北101的路線"


def test_render_arrivals_to_destination_no_eta_row(use_provider):
    """Route serves destination but kiosk row missing → 無即時資料, still listed."""
    use_provider(
        FakeBusProvider(
            route_info={"7000D": {"id": "7000D", "go_dest": "西螺", "back_dest": "雲林科技大學"}},
            route_estimate=[
                {"direction": 0, "stop_sequence": 1, "stop_name": "別的站", "stop_status": 0, "estimate_seconds": 300},
                {"direction": 0, "stop_sequence": 2, "stop_name": "西螺", "stop_status": 0, "estimate_seconds": 900},
            ],
        )
    )
    result = asyncio.run(departures.render_arrivals_to_destination("西螺", "雲林科技大學"))
    # 雲林科技大學 not in stop sequence → no downstream match → no hit
    assert result == "本站沒有直達西螺的路線"


# ── geo-awareness ────────────────────────────────────────────────────────────


def test_render_stop_on_route_rejects_upstream_destination(use_provider):
    """`check_stop_on_route` is geo-aware: upstream destination → 沒有."""
    use_provider(
        FakeBusProvider(
            route_info={
                "201": {"id": "201", "go_dest": "東邊終點", "back_dest": "西邊終點"},
            },
            route_estimate=[
                # 虎尾 is at seq 1, kiosk at seq 2 — upstream from kiosk.
                {"direction": 0, "stop_sequence": 1, "stop_name": "虎尾", "stop_status": 0, "estimate_seconds": 300},
                {"direction": 0, "stop_sequence": 2, "stop_name": "雲林科技大學", "stop_status": 0, "estimate_seconds": 600},
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
                "201": {"id": "201", "go_dest": "東邊終點", "back_dest": "西邊終點"},
            },
            route_estimate=[
                {"direction": 0, "stop_sequence": 1, "stop_name": "雲林科技大學", "stop_status": 0, "estimate_seconds": 0},
                {"direction": 0, "stop_sequence": 2, "stop_name": "斗六", "stop_status": 0, "estimate_seconds": 600},
            ],
        )
    )
    result = asyncio.run(departures.render_stop_on_route("201", "雲林科技大學", "雲林科技大學"))
    assert result.startswith("有")


# ── _is_terminal_direction ────────────────────────────────────────────────────


def test_is_terminal_direction_empty_go_dest_not_circular():
    """Empty go_dest must NOT trigger the circular-route exception.

    Before the fix, _name_matches(stop, "") returned True (empty string is
    a substring of everything), causing is_circular=True and skipping the
    terminal filter for direction=1 even when back_dest matched the kiosk.
    """
    route_info = {
        "201A": {"id": "201A", "go_dest": "", "back_dest": "斗六火車站"},
    }
    # Direction 1 ends at the kiosk → should be treated as terminal
    assert _is_terminal_direction("斗六火車站", route_info, "201A", 1) is True


def test_is_terminal_direction_empty_go_dest_direction0_not_filtered():
    """Direction 0 with empty go_dest should NOT be filtered (unknown terminus)."""
    route_info = {
        "201A": {"id": "201A", "go_dest": "", "back_dest": "斗六火車站"},
    }
    assert _is_terminal_direction("斗六火車站", route_info, "201A", 0) is False


def test_is_terminal_direction_both_empty_not_filtered():
    """Both termini empty → neither direction filtered (safe fallback)."""
    route_info = {"X": {"id": "X", "go_dest": "", "back_dest": ""}}
    assert _is_terminal_direction("斗六火車站", route_info, "X", 0) is False
    assert _is_terminal_direction("斗六火車站", route_info, "X", 1) is False


def test_is_terminal_direction_circular_both_match():
    """Genuine circular route (kiosk matches both termini) → not filtered."""
    route_info = {
        "Y01": {"id": "Y01", "go_dest": "斗六火車站", "back_dest": "斗六火車站"},
    }
    assert _is_terminal_direction("斗六火車站", route_info, "Y01", 0) is False
    assert _is_terminal_direction("斗六火車站", route_info, "Y01", 1) is False


def test_snapshot_filters_inbound_when_back_dest_matches_kiosk(use_provider):
    """Route with empty go_dest and back_dest=kiosk: direction=1 filtered, direction=0 shown."""
    use_provider(
        FakeBusProvider(
            route_info={
                "201A": {"id": "201A", "go_dest": "", "back_dest": "斗六火車站"},
            },
            eta_at_stop=[
                {"sub_route_name": "201A", "direction": 0, "stop_status": 0, "estimate_seconds": 420},
                {"sub_route_name": "201A", "direction": 1, "stop_status": 0, "estimate_seconds": 1260},
            ],
        )
    )
    snapshot = asyncio.run(departures.build_departure_snapshot("斗六火車站", updated_at=_updated_at()))
    route_ids = [(r.route, r.go_back) for r in snapshot.routes]
    assert ("201A", 0) in route_ids  # outbound shown
    assert ("201A", 1) not in route_ids  # inbound to kiosk filtered
