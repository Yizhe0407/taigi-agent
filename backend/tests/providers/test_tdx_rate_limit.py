"""TDX API-key rate limiter: sliding window, interactive reserve, 429 pause.

Time is a fake clock that `sleep` advances, so no test waits on the wall clock.
"""

from __future__ import annotations

import asyncio

import pytest

from providers.tdx_rate_limit import RateLimit, TdxRateLimiter, parse_rate_limit, reset_tdx_rate_limiters, tdx_rate_limiter
from upstream_deadline import UpstreamBudgetExceeded, upstream_deadline


def _limiter(limit: RateLimit) -> tuple[TdxRateLimiter, list[float], list[float]]:
    now = [0.0]
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    return TdxRateLimiter(limit, clock=lambda: now[0], sleep=fake_sleep), sleeps, now


async def _take(limiter: TdxRateLimiter, n: int, *, budget: float | None = None) -> None:
    for _ in range(n):
        if budget is None:
            await limiter.acquire()
        else:
            with upstream_deadline(budget):
                await limiter.acquire()


@pytest.mark.parametrize(
    ("text", "expected"),
    [("5/min", RateLimit(5, 60.0)), ("5/s", RateLimit(5, 1.0)), ("50 / sec", RateLimit(50, 1.0)), ("100/hour", RateLimit(100, 3600.0))],
)
def test_parse_rate_limit(text, expected):
    assert parse_rate_limit(text) == expected


@pytest.mark.parametrize("text", ["5", "five/min", "5/day", "0/s", ""])
def test_parse_rate_limit_rejects_garbage(text):
    with pytest.raises(ValueError):
        parse_rate_limit(text)


@pytest.mark.parametrize(("requests", "background"), [(5, 3), (1, 1), (2, 1), (10, 6), (50, 30)])
def test_background_share_leaves_a_reserve_for_riders(requests, background):
    assert RateLimit(requests, 60.0).background_requests == background


def test_background_callers_wait_once_their_share_is_used():
    """Basic tier 5/min: background gets 3, the 4th waits for the oldest to age out."""
    limiter, sleeps, now = _limiter(RateLimit(5, 60.0))

    async def scenario() -> None:
        await _take(limiter, 3)
        now[0] = 10.0
        await _take(limiter, 1)

    asyncio.run(scenario())
    assert sleeps == [50.0]  # first request at t=0 leaves the window at t=60


def test_riders_can_use_the_reserved_slots_without_waiting():
    limiter, sleeps, _ = _limiter(RateLimit(5, 60.0))

    async def scenario() -> None:
        await _take(limiter, 3)  # background fills its share
        await _take(limiter, 2, budget=3.0)  # riders still get the reserve

    asyncio.run(scenario())
    assert sleeps == []


def test_rider_fails_fast_when_no_slot_opens_within_budget():
    limiter, sleeps, _ = _limiter(RateLimit(5, 60.0))

    async def scenario() -> None:
        await _take(limiter, 5, budget=3.0)
        await _take(limiter, 1, budget=3.0)

    with pytest.raises(UpstreamBudgetExceeded):
        asyncio.run(scenario())
    assert sleeps == []


def test_rider_waits_for_a_slot_that_opens_within_budget():
    """Bronze tier 5/s: a 6th request in the same second waits ~1 s, not fails."""
    limiter, sleeps, _ = _limiter(RateLimit(5, 1.0))

    asyncio.run(_take(limiter, 6, budget=3.0))
    assert sleeps == [1.0]


def test_429_pause_blocks_every_caller_on_the_key():
    limiter, sleeps, _ = _limiter(RateLimit(1000, 1.0))
    limiter.penalize(30.0)

    with pytest.raises(UpstreamBudgetExceeded):
        asyncio.run(_take(limiter, 1, budget=3.0))
    asyncio.run(_take(limiter, 1))  # background sits it out
    assert sleeps == [30.0]


def test_penalize_never_shortens_an_existing_pause():
    limiter, sleeps, _ = _limiter(RateLimit(1000, 1.0))
    limiter.penalize(30.0)
    limiter.penalize(5.0)
    asyncio.run(_take(limiter, 1))
    assert sleeps == [30.0]


def test_limiter_is_shared_per_api_key(monkeypatch):
    monkeypatch.setenv("TDX_RATE_LIMIT", "5/s")
    reset_tdx_rate_limiters()
    assert tdx_rate_limiter("key-a") is tdx_rate_limiter("key-a")
    assert tdx_rate_limiter("key-a") is not tdx_rate_limiter("key-b")
    assert tdx_rate_limiter("key-a").limit == RateLimit(5, 1.0)


def test_default_limit_is_the_basic_tier(monkeypatch):
    monkeypatch.delenv("TDX_RATE_LIMIT", raising=False)
    reset_tdx_rate_limiters()
    assert tdx_rate_limiter("key").limit == RateLimit(5, 60.0)


def test_bus_and_bike_providers_share_one_limiter_per_key(monkeypatch):
    """TDX limits the key, not the endpoint: bus and bike must draw from one budget."""
    from providers.tdx_bike import TdxBikeProvider
    from providers.tdx_bus import TdxBusProvider

    monkeypatch.setenv("TDX_CLIENT_ID", "shared-key")
    monkeypatch.setenv("TDX_CLIENT_SECRET", "secret")
    bus = TdxBusProvider("shared-key", "secret")
    bike = TdxBikeProvider()
    asyncio.run(bike._ensure_token_client())
    assert bike._rate_limiter is bus._limiter is tdx_rate_limiter("shared-key")
