from __future__ import annotations

import asyncio

from upstream_deadline import remaining_budget, upstream_deadline


def test_no_budget_outside_a_deadline():
    assert remaining_budget() is None


def test_budget_is_bounded_and_restored():
    with upstream_deadline(3.0):
        remaining = remaining_budget()
        assert remaining is not None and 0 < remaining <= 3.0
    assert remaining_budget() is None


def test_nested_budget_never_extends_the_outer_one():
    with upstream_deadline(1.0):
        with upstream_deadline(10.0):
            remaining = remaining_budget()
            assert remaining is not None and remaining <= 1.0


def test_spent_budget_reports_zero_not_negative():
    with upstream_deadline(0.0):
        assert remaining_budget() == 0.0


def test_budget_follows_the_request_into_gathered_tasks():
    async def seen() -> float | None:
        return remaining_budget()

    async def scenario() -> list[float | None]:
        with upstream_deadline(3.0):
            return list(await asyncio.gather(seen(), seen()))

    assert all(r is not None and r <= 3.0 for r in asyncio.run(scenario()))
