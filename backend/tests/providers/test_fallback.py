"""Tests for the provider-neutral ordered fallback composer."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from providers.bus import Direction, RouteAtStop, RouteInfo, RouteStopEstimate, StopArrival, StopStatus
from providers.fallback import FallbackBusProvider

_UNSET = object()


def _fake_provider(*, route_estimate=None, eta=_UNSET, info=None, routes=None):
    provider = MagicMock()
    provider.fetch_route_estimate = AsyncMock(return_value=route_estimate)
    provider.fetch_eta_at_stop = AsyncMock(return_value=[] if eta is _UNSET else eta)
    provider.load_route_info = AsyncMock(return_value=info or {})
    provider.fetch_routes_at_stop = AsyncMock(return_value=routes or [])
    return provider


def _make_fallback(
    *,
    primary_route_estimate=None,
    primary_eta=_UNSET,
    primary_info=None,
    primary_routes=None,
    fallback_route_estimate=None,
    fallback_eta=None,
    fallback_info=None,
    fallback_routes=None,
):
    primary = _fake_provider(
        route_estimate=primary_route_estimate,
        eta=primary_eta,
        info=primary_info,
        routes=primary_routes,
    )
    fallback = _fake_provider(
        route_estimate=fallback_route_estimate,
        eta=fallback_eta,
        info=fallback_info,
        routes=fallback_routes,
    )
    return FallbackBusProvider([primary, fallback]), primary, fallback


def _stop(name: str, direction: Direction = Direction.OUTBOUND, eta: int | None = 120) -> RouteStopEstimate:
    return RouteStopEstimate(
        stop_name=name,
        sequence=1,
        direction=direction,
        status=StopStatus.AVAILABLE,
        eta_seconds=eta,
    )


def test_rejects_an_empty_chain():
    with pytest.raises(ValueError, match="at least one"):
        FallbackBusProvider([])


def test_fetch_route_estimate_uses_primary_when_available():
    rows = [_stop("A")]
    provider, primary, fallback = _make_fallback(primary_route_estimate=rows)

    result = asyncio.run(provider.fetch_route_estimate("Y01"))

    assert result == rows
    primary.fetch_route_estimate.assert_awaited_once_with("Y01")
    fallback.fetch_route_estimate.assert_not_awaited()


def test_fetch_route_estimate_falls_back_when_primary_returns_none():
    rows = [_stop("B", Direction.INBOUND, 60)]
    provider, primary, fallback = _make_fallback(fallback_route_estimate=rows)

    result = asyncio.run(provider.fetch_route_estimate("Y99"))

    assert result == rows
    primary.fetch_route_estimate.assert_awaited_once_with("Y99")
    fallback.fetch_route_estimate.assert_awaited_once_with("Y99")


def test_fetch_route_estimate_raises_when_the_chain_ends_on_a_failure():
    provider, _, fallback = _make_fallback()
    fallback.fetch_route_estimate.side_effect = RuntimeError("down")

    with pytest.raises(RuntimeError, match="down"):
        asyncio.run(provider.fetch_route_estimate("Y01"))


def test_fetch_eta_uses_primary_empty_as_a_real_answer():
    provider, primary, fallback = _make_fallback(primary_eta=[])

    assert asyncio.run(provider.fetch_eta_at_stop("斗六火車站")) == []
    primary.fetch_eta_at_stop.assert_awaited_once_with("斗六火車站")
    fallback.fetch_eta_at_stop.assert_not_awaited()


def test_fetch_eta_falls_back_only_when_primary_is_unavailable():
    rows = [StopArrival(route_name="7120", direction=Direction.OUTBOUND, status=StopStatus.AVAILABLE, eta_seconds=300)]
    provider, primary, fallback = _make_fallback(primary_eta=None, fallback_eta=rows)

    result = asyncio.run(provider.fetch_eta_at_stop("斗六火車站"))

    assert result == rows
    primary.fetch_eta_at_stop.assert_awaited_once_with("斗六火車站")
    fallback.fetch_eta_at_stop.assert_awaited_once_with("斗六火車站")


def test_fetch_eta_returns_empty_when_both_providers_fail():
    provider, primary, fallback = _make_fallback(primary_eta=None)
    primary.fetch_eta_at_stop.side_effect = RuntimeError("down")
    fallback.fetch_eta_at_stop.side_effect = RuntimeError("down")

    assert asyncio.run(provider.fetch_eta_at_stop("斗六火車站")) == []


def test_load_route_info_prefers_complete_primary():
    info = {"101": RouteInfo("101", "斗六棒球場", "受天宮")}
    provider, _, fallback = _make_fallback(primary_info=info)

    assert asyncio.run(provider.load_route_info("斗六火車站")) == info
    fallback.load_route_info.assert_not_awaited()


def test_load_route_info_merges_missing_primary_destinations():
    primary_info = {"101": RouteInfo("101", "", "受天宮")}
    fallback_info = {"101": RouteInfo("101", "斗六棒球場", "")}
    provider, _, fallback = _make_fallback(primary_info=primary_info, fallback_info=fallback_info)

    result = asyncio.run(provider.load_route_info("斗六火車站"))

    assert result["101"] == RouteInfo("101", "斗六棒球場", "受天宮")
    fallback.load_route_info.assert_awaited_once_with("斗六火車站")


def test_fetch_routes_prefers_primary_and_falls_back_when_empty():
    routes = [RouteAtStop(route_name="Y01", direction=Direction.OUTBOUND)]
    provider, _, fallback = _make_fallback(primary_routes=routes)

    assert asyncio.run(provider.fetch_routes_at_stop("斗六火車站")) == routes
    fallback.fetch_routes_at_stop.assert_not_awaited()

    provider, _, fallback = _make_fallback(fallback_routes=routes)
    assert asyncio.run(provider.fetch_routes_at_stop("斗六火車站")) == routes
    fallback.fetch_routes_at_stop.assert_awaited_once_with("斗六火車站")


def test_fetch_routes_raises_when_the_chain_ends_on_a_failure():
    provider, _, fallback = _make_fallback()
    fallback.fetch_routes_at_stop.side_effect = RuntimeError("down")

    with pytest.raises(RuntimeError, match="down"):
        asyncio.run(provider.fetch_routes_at_stop("斗六火車站"))


def test_chain_is_not_limited_to_two_providers():
    rows = [_stop("C")]
    first = _fake_provider()
    second = _fake_provider()
    third = _fake_provider(route_estimate=rows)
    provider = FallbackBusProvider([first, second, third])

    assert asyncio.run(provider.fetch_route_estimate("Y01")) == rows
    third.fetch_route_estimate.assert_awaited_once_with("Y01")


def test_route_info_merges_across_more_than_two_providers():
    first = _fake_provider(info={"101": RouteInfo("101", "", "")})
    second = _fake_provider(info={"101": RouteInfo("101", "斗六棒球場", "")})
    third = _fake_provider(info={"101": RouteInfo("101", "", "受天宮")})
    provider = FallbackBusProvider([first, second, third])

    result = asyncio.run(provider.load_route_info("斗六火車站"))

    assert result["101"] == RouteInfo("101", "斗六棒球場", "受天宮")


def test_provider_fallback_metrics_are_provider_neutral():
    provider, _, _ = _make_fallback(primary_route_estimate=[_stop("A")])
    with patch("providers.fallback.get_telemetry") as telemetry:
        asyncio.run(provider.fetch_route_estimate("Y01"))
    telemetry.return_value.record_provider_fallback.assert_called_once_with(operation="route_estimate", outcome="primary_hit")
