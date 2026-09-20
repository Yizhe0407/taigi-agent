"""Provider-neutral ordered composition of bus providers.

This module knows only the ``BusProvider`` contract.  It must not import or
name any concrete upstream client; adding TaiwanBus, a regional client, or a
test provider therefore requires no change here.

Each operation defines its own "this source has nothing useful" signal, because
the contract gives them different meanings:

- ``fetch_routes_at_stop``: an empty list means the source does not know the
  stop, so the chain keeps looking.
- ``fetch_eta_at_stop`` / ``fetch_route_estimate``: ``None`` means unavailable /
  unsupported; an empty list is a real answer ("up, but nothing to show") and
  ends the chain.
- ``load_route_info``: results merge across sources until every route has both
  terminal labels.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from providers.bus import (
    BusProvider,
    RouteAtStop,
    RouteInfo,
    RouteStopEstimate,
    StopArrival,
)
from telemetry import get_telemetry

_log = logging.getLogger(__name__)


def _provider_name(provider: BusProvider) -> str:
    name = getattr(provider, "name", None)
    return name if isinstance(name, str) and name else type(provider).__name__


class FallbackBusProvider(BusProvider):
    """Query an ordered chain of providers until one answers usefully."""

    name = "fallback"

    def __init__(self, providers: Sequence[BusProvider]) -> None:
        if not providers:
            raise ValueError("at least one bus provider is required")
        self._providers = tuple(providers)

    @property
    def providers(self) -> tuple[BusProvider, ...]:
        return self._providers

    async def fetch_routes_at_stop(self, stop_name: str) -> list[RouteAtStop]:
        last_error: Exception | None = None
        for index, provider in enumerate(self._providers):
            try:
                rows = await provider.fetch_routes_at_stop(stop_name)
            except Exception as exc:  # noqa: BLE001 — provider boundary must isolate upstream failures
                last_error = exc
                _log.warning("%s route lookup failed for %s: %s", _provider_name(provider), stop_name, exc)
                continue
            last_error = None
            if rows:
                self._record("routes", index)
                return list(rows)

        self._record("routes", None)
        # The chain ended on a failure rather than on a genuine "no routes
        # here", so surface it instead of claiming the stop has no routes.
        if last_error is not None:
            raise last_error
        return []

    async def fetch_eta_at_stop(self, stop_name: str) -> list[StopArrival]:
        for index, provider in enumerate(self._providers):
            try:
                rows = await provider.fetch_eta_at_stop(stop_name)
            except Exception as exc:  # noqa: BLE001 — provider boundary must isolate upstream failures
                _log.warning("%s ETA lookup failed for %s: %s", _provider_name(provider), stop_name, exc)
                continue
            if rows is not None:
                self._record("eta", index)
                return list(rows)

        self._record("eta", None)
        return []

    async def fetch_route_estimate(self, route_name: str) -> list[RouteStopEstimate] | None:
        last_error: Exception | None = None
        for index, provider in enumerate(self._providers):
            try:
                rows = await provider.fetch_route_estimate(route_name)
            except Exception as exc:  # noqa: BLE001 — provider boundary must isolate upstream failures
                last_error = exc
                _log.warning("%s route estimate failed for %s: %s", _provider_name(provider), route_name, exc)
                continue
            last_error = None
            if rows is not None:
                self._record("route_estimate", index)
                return list(rows)

        self._record("route_estimate", None)
        if last_error is not None:
            raise last_error
        return None

    async def load_route_info(self, stop_name: str) -> dict[str, RouteInfo]:
        merged: dict[str, RouteInfo] = {}
        hit_index: int | None = None
        last_error: Exception | None = None
        for index, provider in enumerate(self._providers):
            try:
                info = await provider.load_route_info(stop_name)
            except Exception as exc:  # noqa: BLE001 — provider boundary must isolate upstream failures
                last_error = exc
                _log.warning("%s route-info lookup failed for %s: %s", _provider_name(provider), stop_name, exc)
                continue
            last_error = None
            if info:
                merged = _merge_route_info(merged, info)
                if hit_index is None:
                    hit_index = index
            # A complete result avoids further upstream calls.  Anything short
            # of that (no routes, or missing terminal labels) asks the next
            # source to fill the gaps; no concrete provider knowledge needed.
            if merged and _is_complete(merged):
                break

        if merged:
            self._record("route_info", hit_index)
            return merged
        self._record("route_info", None)
        if last_error is not None:
            raise last_error
        return {}

    @staticmethod
    def _record(operation: str, index: int | None) -> None:
        if index is None:
            outcome = "both_empty"
        else:
            outcome = "primary_hit" if index == 0 else "fallback_hit"
        get_telemetry().record_provider_fallback(operation=operation, outcome=outcome)


def _is_complete(info: dict[str, RouteInfo]) -> bool:
    return all(route.outbound_destination and route.inbound_destination for route in info.values())


def _merge_route_info(base: dict[str, RouteInfo], extra: dict[str, RouteInfo]) -> dict[str, RouteInfo]:
    """Merge ``extra`` under ``base``; earlier sources win field by field."""
    merged = dict(extra)
    for route_name, info in base.items():
        other = extra.get(route_name)
        if other is None:
            merged[route_name] = info
            continue
        merged[route_name] = RouteInfo(
            route_name=route_name,
            outbound_destination=info.outbound_destination or other.outbound_destination,
            inbound_destination=info.inbound_destination or other.inbound_destination,
        )
    return merged
