from __future__ import annotations

import asyncio

import api.departures as departures_api
from upstream_deadline import remaining_budget


def test_kiosk_screen_reads_run_under_an_upstream_budget(monkeypatch):
    """The screen must answer from cache on a rate-limited TDX, not queue for a slot."""
    seen: list[float | None] = []

    async def fake_snapshot(*args, **kwargs):
        seen.append(remaining_budget())

    async def fake_detail(*args, **kwargs):
        seen.append(remaining_budget())

    monkeypatch.setattr(departures_api, "build_departure_snapshot", fake_snapshot)
    monkeypatch.setattr(departures_api, "build_route_detail", fake_detail)
    asyncio.run(departures_api.get_departure_snapshot_here())
    asyncio.run(departures_api.get_route_detail_here("201"))

    budget = departures_api.HTTP_UPSTREAM_BUDGET_SECONDS
    assert len(seen) == 2 and all(r is not None and 0 < r <= budget for r in seen)
