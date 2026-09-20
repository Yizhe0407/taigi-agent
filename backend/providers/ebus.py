"""ebus.yunlin.gov.tw unofficial city bus API client.

No authentication required. No observed rate limits.

API shape (route-scoped, numeric route ID in URL):
  GET /api/route               → [{Id, NameZh, DepartureZh, DestinationZh, ...}]
  GET /api/route/{id}/estimate → [{SID, StopName, GoBack, Value, SeqNo, ComeTime, ESTs, ...}]

Direction encoding differs from TDX:
  ebus GoBack: 1=去程, 2=回程
  TDX direction: 0=去程, 1=回程
  Conversion: direction = GoBack - 1

ETA value:
  ebus Value: int (minutes), null, or negative sentinel
    >= 0  → bus approaching; estimate_seconds = Value * 60
    null  → 未發車 (first bus not yet departed)
    < 0   → 末班已過 (e.g. Value == -3)
  TDX estimate_seconds: int (seconds) or None
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from async_lifecycle import ReclaimingAsyncLock, run_in_thread
from providers.bus import (
    BusProvider,
    Direction,
    RouteAtStop,
    RouteInfo,
    RouteStopEstimate,
    StopArrival,
    StopStatus,
)
from providers.http import get_http_client
from providers.ttl_cache import TtlCache

_log = logging.getLogger(__name__)

_BASE = "https://ebus.yunlin.gov.tw/api"
_ROUTE_MAP_TTL = 86400.0  # route list changes at most daily
_ESTIMATE_TTL = 30.0  # real-time; same cadence as TDX
_STOP_ROUTE_TTL = 86400.0  # which routes serve a stop changes at most daily
_STOP_ROUTE_PARTIAL_TTL = 60.0  # short TTL when the scan had per-route failures
_ROUTE_INDEX_SCHEMA_VERSION = 2

# Strips trailing alpha/Chinese-character suffixes used in sub-route variants:
# "7120A" → "7120", "101甲" → "101". Used as fallback when exact name not found.
_SUFFIX_RE = re.compile(r"[A-Z甲乙丙丁區]+$")


# ── Row normalisers ────────────────────────────────────────────────────────────


def _norm_route_estimate_row(row: dict, route_name: str) -> RouteStopEstimate:
    """Translate one native Ebus row into the provider-neutral model."""
    value = row.get("Value")
    if value is None:
        status = StopStatus.NOT_DEPARTED
        eta_seconds = None
    elif value >= 0:
        status = StopStatus.AVAILABLE
        eta_seconds = value * 60
    else:
        status = StopStatus.LAST_DEPARTED
        eta_seconds = None
    return RouteStopEstimate(
        stop_name=str(row.get("StopName") or ""),
        sequence=row.get("SeqNo"),
        direction=Direction((row.get("GoBack") or 1) - 1),
        status=status,
        eta_seconds=eta_seconds,
        scheduled_time=row.get("ComeTime"),
        vehicle_id=row.get("CarId") or None,
        route_name=route_name,
    )


def _route_est_to_eta(row: RouteStopEstimate, route_name: str) -> StopArrival:
    """Convert a route-stop estimate into a stop arrival."""
    return StopArrival(
        route_name=route_name,
        direction=row.direction,
        status=row.status,
        eta_seconds=row.eta_seconds,
        sequence=row.sequence,
        scheduled_time=row.scheduled_time,
        vehicle_id=row.vehicle_id,
    )


def _dedup_eta_by_min_seq(rows: list[StopArrival]) -> list[StopArrival]:
    """Keep one row per route/direction; the boarding occurrence wins."""
    best: dict[tuple[str, Direction], StopArrival] = {}
    for row in rows:
        key = (row.route_name, row.direction)
        seq = row.sequence or 9999
        existing = best.get(key)
        if existing is None or (existing.sequence or 9999) > seq:
            best[key] = row
    return list(best.values())


# ── Route terminal helper ──────────────────────────────────────────────────────


def _route_info_from_estimate(route_name: str, rows: list[RouteStopEstimate]) -> RouteInfo:
    """Derive outbound/inbound terminals from an ordered route estimate."""
    best: dict[Direction, tuple[int, str]] = {}
    for row in rows:
        seq = row.sequence or 0
        if not row.stop_name:
            continue
        current = best.get(row.direction)
        if current is None or seq > current[0]:
            best[row.direction] = (seq, row.stop_name)
    return RouteInfo(
        route_name=route_name,
        outbound_destination=best.get(Direction.OUTBOUND, (0, ""))[1],
        inbound_destination=best.get(Direction.INBOUND, (0, ""))[1],
    )


# ── Provider ───────────────────────────────────────────────────────────────────


class EbusBusProvider(BusProvider):
    """HTTP client for ebus.yunlin.gov.tw city route ETA.

    Implements the provider-neutral bus contract; composition and fallback live in
    ``FallbackBusProvider`` and do not depend on this concrete client. The route map is cached for 24 h; individual
    route estimates are cached for 30 s (same as TDX ETA TTL).
    """

    def __init__(
        self,
        *,
        route_map_ttl: float = _ROUTE_MAP_TTL,
        estimate_ttl: float = _ESTIMATE_TTL,
        stop_route_ttl: float = _STOP_ROUTE_TTL,
        clock: Callable[[], float] = time.monotonic,
        route_index_path: Path | str | None = None,
    ) -> None:
        self._route_map_ttl = route_map_ttl
        self._estimate_ttl = estimate_ttl
        self._stop_route_ttl = stop_route_ttl
        self._clock = clock
        # (fetched_at, {NameZh: route_id})
        self._route_map: tuple[float, dict[str, int]] | None = None
        # route_id → (fetched_at, normalised route-estimate rows)
        self._estimate_cache: dict[int, tuple[float, list[RouteStopEstimate]]] = {}
        self._estimate_ttl_cache: TtlCache[int, list[RouteStopEstimate]] = TtlCache(self._estimate_cache, clock=self._clock, cache_name="ebus route estimate")
        # stop_name → (fetched_at, {route_name: RouteInfo}, had_failures)
        # had_failures=True uses _STOP_ROUTE_PARTIAL_TTL so a route that failed
        # mid-scan gets re-checked soon instead of staying "missing" for 24h.
        #
        # Deliberately NOT on `TtlCache` (unlike `_estimate_cache` above), because
        # every part of that class's contract is wrong here:
        #   - entries are 3-tuples with a per-entry TTL chosen by `had_failures`;
        #     TtlCache stores 2-tuples under one caller-supplied scalar TTL.
        #   - one scan populates *every* stop key at once, so all misses must
        #     collapse into a single global lock. TtlCache locks per key, which
        #     would let two cold stop names launch two full route scans.
        #   - stale-serve fires when the scan *succeeds but returns empty* for
        #     this stop, not when the fetch raises, and it is unbounded rather
        #     than capped by a `stale_ttl`.
        #   - `_maybe_sweep` would evict entries this cache intends to keep for
        #     the full 24 h retention that backs the on-disk route index.
        # Bending TtlCache to cover all four would add flags to a class shared by
        # the TDX ETA path, where its per-key coalescing is what keeps the 429
        # cascade from returning. The hand-rolled lock stays.
        self._stop_route_cache: dict[str, tuple[float, dict[str, RouteInfo], bool]] = {}
        # A single scan builds every stop, so all misses share one lock.
        self._route_index_lock = ReclaimingAsyncLock("ebus route-index refresh")
        self._route_index_path = Path(route_index_path) if route_index_path is not None else None
        self._load_persisted_route_index()

    def _expired(self, fetched_at: float, ttl: float) -> bool:
        return (self._clock() - fetched_at) >= ttl

    async def _get(self, url: str) -> list[dict]:
        http = get_http_client()
        resp = await http.get(url, timeout=10.0)
        resp.raise_for_status()
        return resp.json()

    async def _load_route_map(self) -> dict[str, int]:
        """GET /api/route → {NameZh: Id}. Cached 24 h."""
        if self._route_map is not None and not self._expired(self._route_map[0], self._route_map_ttl):
            return self._route_map[1]
        raw = await self._get(f"{_BASE}/route")
        mapping = {r["NameZh"]: int(r["Id"]) for r in raw if r.get("NameZh") and r.get("Id") is not None}
        self._route_map = (self._clock(), mapping)
        return mapping

    async def get_route_id(self, route_name: str) -> int | None:
        """Return the ebus numeric route ID, or None if this route is not in ebus.

        Tries exact NameZh match first, then strips trailing ASCII/Chinese
        suffixes ("7120A" → "7120", "101甲" → "101") as a fallback.
        """
        route_map = await self._load_route_map()
        if route_name in route_map:
            return route_map[route_name]
        base = _SUFFIX_RE.sub("", route_name)
        if base != route_name:
            return route_map.get(base)
        return None

    def _load_persisted_route_index(self) -> None:
        path = self._route_index_path
        if path is None:
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            saved_at = float(payload["saved_at"])
            stops = payload["stops"]
            if payload.get("schema_version") != _ROUTE_INDEX_SCHEMA_VERSION or not isinstance(stops, dict):
                raise ValueError("unsupported route index schema")
            if time.time() - saved_at >= self._stop_route_ttl:
                return
            fetched_at = self._clock()
            for stop_name, info in stops.items():
                if isinstance(stop_name, str) and isinstance(info, dict):
                    normalized = {
                        str(route_name): RouteInfo(
                            route_name=str(route_name),
                            outbound_destination=str(route_info.get("outbound_destination") or ""),
                            inbound_destination=str(route_info.get("inbound_destination") or ""),
                        )
                        for route_name, route_info in info.items()
                        if isinstance(route_info, dict)
                    }
                    self._stop_route_cache[stop_name] = (fetched_at, normalized, False)
        except FileNotFoundError:
            return
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as error:
            _log.warning("Ignoring invalid persisted ebus route index %s: %s", path, error)

    def _write_persisted_route_index(self, stops: dict[str, dict[str, RouteInfo]]) -> None:
        path = self._route_index_path
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(
                json.dumps(
                    {
                        "schema_version": _ROUTE_INDEX_SCHEMA_VERSION,
                        "saved_at": time.time(),
                        "stops": {
                            stop_name: {
                                route_name: {
                                    "outbound_destination": route_info.outbound_destination,
                                    "inbound_destination": route_info.inbound_destination,
                                }
                                for route_name, route_info in route_info_by_name.items()
                            }
                            for stop_name, route_info_by_name in stops.items()
                        },
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _stop_route_fresh(self, cached: tuple[float, dict[str, RouteInfo], bool]) -> bool:
        """True if a `_stop_route_cache` entry is still within its TTL.

        `had_failures` (cached[2]) selects the short partial TTL so a route
        that failed mid-scan gets re-checked soon instead of staying
        "missing" for a full day.
        """
        ttl = self._stop_route_ttl if not cached[2] else _STOP_ROUTE_PARTIAL_TTL
        return not self._expired(cached[0], ttl)

    async def _scan_all_routes(self) -> tuple[dict[str, dict[str, RouteInfo]], bool]:
        """Fetch every known route's estimate (20 concurrent) and index stops by name.

        Returns ``{stop_name: {route_name: RouteInfo}}`` plus the failure flag.
        """
        route_map = await self._load_route_map()
        names = list(route_map.keys())
        sem = asyncio.Semaphore(20)

        async def _check(name: str) -> tuple[RouteInfo, set[str]] | None:
            async with sem:
                rows = await self.fetch_route_estimate(name)
                if not rows:
                    return None
                stop_names = {row.stop_name.strip() for row in rows}
                stop_names.discard("")
                return _route_info_from_estimate(name, rows), stop_names

        results = await asyncio.gather(*[_check(n) for n in names], return_exceptions=True)
        route_index: dict[str, dict[str, RouteInfo]] = {}
        had_failures = False
        for name, result in zip(names, results):
            if isinstance(result, BaseException):
                _log.warning("ebus route scan failed for %s: %s", name, result)
                had_failures = True
            elif result is not None:
                route_info, stop_names = result
                for exact_stop_name in stop_names:
                    route_index.setdefault(exact_stop_name, {})[name] = route_info
        return route_index, had_failures

    async def find_routes_at_stop(self, stop_name: str) -> dict[str, RouteInfo]:
        """Discover all routes serving stop_name by scanning all route estimates. Cached 24h
        (or 60s if the scan had per-route failures — see `_STOP_ROUTE_PARTIAL_TTL`).

        Returns ``route_name -> RouteInfo``.
        Covers city routes and 7xxx intercity routes alike.
        """
        cached = self._stop_route_cache.get(stop_name)
        if cached is not None and self._stop_route_fresh(cached):
            return cached[1]

        async with self._route_index_lock.acquire():
            # Re-check after acquiring: the first caller builds every stop.
            cached = self._stop_route_cache.get(stop_name)
            if cached is not None and self._stop_route_fresh(cached):
                return cached[1]

            route_index, had_failures = await self._scan_all_routes()
            info = route_index.get(stop_name.strip(), {})

            if not info and cached is not None:
                _log.warning("ebus route scan returned empty; serving stale for %s", stop_name)
                return cached[1]

            fetched_at = self._clock()
            for exact_stop_name, exact_info in route_index.items():
                self._stop_route_cache[exact_stop_name] = (fetched_at, exact_info, had_failures)
            self._stop_route_cache.setdefault(stop_name, (fetched_at, {}, had_failures))
            if not had_failures and route_index:
                try:
                    await run_in_thread(self._write_persisted_route_index, route_index)
                except OSError as error:
                    _log.warning("Unable to persist ebus route index: %s", error)
            return info

    async def load_route_info(self, stop_name: str) -> dict[str, RouteInfo]:
        return await self.find_routes_at_stop(stop_name)

    async def fetch_routes_at_stop(self, stop_name: str) -> list[RouteAtStop]:
        info = await self.load_route_info(stop_name)
        return [RouteAtStop(route_name=name, direction=direction) for name in info for direction in (Direction.OUTBOUND, Direction.INBOUND)]

    async def fetch_eta_at_stop(self, stop_name: str) -> list[StopArrival] | None:
        info = await self.load_route_info(stop_name)
        return await self.fetch_eta_rows_for_stop(stop_name, list(info))

    async def fetch_route_estimate(self, route_name: str) -> list[RouteStopEstimate] | None:
        """ETA rows for every stop along route_name, or None if not in ebus.

        Returns None (not raises) so provider fallback composer can silently fall back
        to another provider for routes this source does not cover.
        """
        route_id = await self.get_route_id(route_name)
        if route_id is None:
            return None

        async def _fetch() -> list[RouteStopEstimate]:
            raw = await self._get(f"{_BASE}/route/{route_id}/estimate")
            return [_norm_route_estimate_row(r, route_name) for r in raw]

        rows = await self._estimate_ttl_cache.get_or_fetch(route_id, _fetch, ttl=self._estimate_ttl)
        return list(rows)

    async def fetch_eta_rows_for_stop(
        self,
        stop_name: str,
        route_names: list[str],
    ) -> list[StopArrival] | None:
        """ETA rows at stop_name for the given city routes, or None if every
        route query failed outright.

        Fetches all route estimates in parallel (no rate limit). Each route's
        estimate is cached and shared with fetch_route_estimate callers.
        Per-route failures are logged and skipped (partial degradation) as
        long as at least one route succeeded; an empty list is then a
        genuine "no ETA" answer, not a fetch failure. Only when every route
        query raised is None returned, so callers (provider fallback composer) can
        tell "ebus is down" apart from "ebus is up but has nothing to show".
        """

        async def _one(name: str) -> list[StopArrival]:
            route_rows = await self.fetch_route_estimate(name)
            if route_rows is None:
                return []
            matching = [r for r in route_rows if r.stop_name.strip() == stop_name.strip()]
            return _dedup_eta_by_min_seq([_route_est_to_eta(r, name) for r in matching])

        if not route_names:
            return []

        tasks = [_one(name) for name in route_names]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        if all(isinstance(result, BaseException) for result in results):
            for name, result in zip(route_names, results):
                _log.warning("ebus ETA failed for %s: %s", name, result)
            return None
        rows: list[StopArrival] = []
        for name, result in zip(route_names, results):
            if isinstance(result, list):
                rows.extend(result)
            else:
                _log.warning("ebus ETA failed for %s: %s", name, result)
        return rows
