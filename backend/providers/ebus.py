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
import logging
import re
import time
from collections.abc import Callable

from providers.http import get_http_client

_log = logging.getLogger(__name__)

_BASE = "https://ebus.yunlin.gov.tw/api"
_ROUTE_MAP_TTL = 86400.0  # route list changes at most daily
_ESTIMATE_TTL = 30.0  # real-time; same cadence as TDX
_STOP_ROUTE_TTL = 86400.0  # which routes serve a stop changes at most daily

# Strips trailing alpha/Chinese-character suffixes used in sub-route variants:
# "7120A" → "7120", "101甲" → "101". Used as fallback when exact name not found.
_SUFFIX_RE = re.compile(r"[A-Z甲乙丙丁區]+$")


# ── Row normalisers ────────────────────────────────────────────────────────────


def _norm_route_estimate_row(row: dict) -> dict:
    """Raw ebus estimate row → fetch_route_estimate format.

    Value >= 0  → 正常/接近站 (status 0)
    Value < 0   → 末班已過 (status 3); ebus uses -3 as sentinel
    Value null  → 未發車 (status 1)
    """
    value = row.get("Value")
    if value is None:
        stop_status = 1  # 未發車
        estimate_seconds = None
    elif value >= 0:
        stop_status = 0
        estimate_seconds = value * 60
    else:  # negative sentinel, e.g. -3
        stop_status = 3  # 末班已過
        estimate_seconds = None
    return {
        "stop_name": row.get("StopName", ""),
        "stop_sequence": row.get("SeqNo"),
        "direction": row["GoBack"] - 1,
        "stop_status": stop_status,
        "estimate_seconds": estimate_seconds,
        "scheduled_time": row.get("ComeTime"),  # HH:MM of next scheduled departure; None when no service
        "car_id": row.get("CarId") or None,
    }


def _route_est_to_eta(row: dict, sub_route_name: str) -> dict:
    """Cached route-estimate row → fetch_eta_at_stop format."""
    return {
        "sub_route_name": sub_route_name,
        "direction": row["direction"],
        "stop_status": row["stop_status"],
        "estimate_seconds": row["estimate_seconds"],
        "stop_sequence": row.get("stop_sequence"),
        "scheduled_time": row.get("scheduled_time"),
        "car_id": row.get("car_id"),
    }


def _dedup_eta_by_min_seq(rows: list[dict]) -> list[dict]:
    """Keep one row per (sub_route_name, direction) — minimum stop_sequence wins.

    Handles circular routes where the kiosk stop appears at two sequence
    positions (boarding point at seq=1 and loop-completion at seq=N).
    """
    best: dict[tuple[str, int], dict] = {}
    for row in rows:
        key = (row.get("sub_route_name", ""), row.get("direction", 0))
        seq = row.get("stop_sequence") or 9999
        existing = best.get(key)
        if existing is None or (existing.get("stop_sequence") or 9999) > seq:
            best[key] = row
    return list(best.values())


# ── Route terminal helper ──────────────────────────────────────────────────────


def _terminals_from_estimate(rows: list[dict]) -> dict[str, str]:
    """Derive go_dest/back_dest from a full route estimate (all stops, both directions).

    Terminal = stop with the highest stop_sequence for each direction.
    """
    best: dict[int, tuple[int, str]] = {}  # direction → (max_seq, stop_name)
    for row in rows:
        direction = row.get("direction", 0)
        seq = row.get("stop_sequence") or 0
        name = row.get("stop_name", "")
        if not name:
            continue
        cur = best.get(direction)
        if cur is None or seq > cur[0]:
            best[direction] = (seq, name)
    return {
        "go_dest": best.get(0, (0, ""))[1],
        "back_dest": best.get(1, (0, ""))[1],
    }


# ── Provider ───────────────────────────────────────────────────────────────────


class EbusBusProvider:
    """HTTP client for ebus.yunlin.gov.tw city route ETA.

    Designed to be composed inside HybridBusProvider so that TDX rate limits
    are not hit for city routes. The route map is cached for 24 h; individual
    route estimates are cached for 30 s (same as TDX ETA TTL).
    """

    def __init__(
        self,
        *,
        route_map_ttl: float = _ROUTE_MAP_TTL,
        estimate_ttl: float = _ESTIMATE_TTL,
        stop_route_ttl: float = _STOP_ROUTE_TTL,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._route_map_ttl = route_map_ttl
        self._estimate_ttl = estimate_ttl
        self._stop_route_ttl = stop_route_ttl
        self._clock = clock
        # (fetched_at, {NameZh: route_id})
        self._route_map: tuple[float, dict[str, int]] | None = None
        # route_id → (fetched_at, normalised route-estimate rows)
        self._estimate_cache: dict[int, tuple[float, list[dict]]] = {}
        # stop_name → (fetched_at, {route_name: {id, go_dest, back_dest}})
        self._stop_route_cache: dict[str, tuple[float, dict[str, dict]]] = {}

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

    async def get_route_id(self, sub_route_name: str) -> int | None:
        """Return the ebus numeric route ID, or None if this route is not in ebus.

        Tries exact NameZh match first, then strips trailing ASCII/Chinese
        suffixes ("7120A" → "7120", "101甲" → "101") as a fallback.
        """
        route_map = await self._load_route_map()
        if sub_route_name in route_map:
            return route_map[sub_route_name]
        base = _SUFFIX_RE.sub("", sub_route_name)
        if base != sub_route_name:
            return route_map.get(base)
        return None

    async def warmup_route_map(self) -> None:
        """Pre-fetch /api/route at startup so the first fetch_route_estimate is warm."""
        await self._load_route_map()

    async def find_routes_at_stop(self, stop_name: str) -> dict[str, dict]:
        """Discover all routes serving stop_name by scanning all route estimates. Cached 24h.

        Returns {sub_route_name: {id, go_dest, back_dest}}.
        Covers city routes and 7xxx intercity routes alike.
        """
        cached = self._stop_route_cache.get(stop_name)
        if cached is not None and not self._expired(cached[0], self._stop_route_ttl):
            return cached[1]

        route_map = await self._load_route_map()
        names = list(route_map.keys())
        sem = asyncio.Semaphore(20)

        async def _check(name: str) -> dict | None:
            async with sem:
                rows = await self.fetch_route_estimate(name)
                if not rows:
                    return None
                if not any(stop_name in r.get("stop_name", "") for r in rows):
                    return None
                terminals = _terminals_from_estimate(rows)
                return {"id": name, **terminals}

        results = await asyncio.gather(*[_check(n) for n in names], return_exceptions=True)
        info: dict[str, dict] = {}
        for name, result in zip(names, results):
            if isinstance(result, BaseException):
                _log.warning("ebus route scan failed for %s: %s", name, result)
            elif result is not None:
                info[name] = result

        if not info and cached is not None:
            _log.warning("ebus route scan returned empty; serving stale for %s", stop_name)
            return cached[1]

        self._stop_route_cache[stop_name] = (self._clock(), info)
        return info

    async def fetch_route_estimate(self, sub_route_name: str) -> list[dict] | None:
        """ETA rows for every stop along sub_route_name, or None if not in ebus.

        Returns None (not raises) so HybridBusProvider can silently fall back
        to TDX for routes ebus does not cover.
        """
        route_id = await self.get_route_id(sub_route_name)
        if route_id is None:
            return None

        cached = self._estimate_cache.get(route_id)
        if cached is not None and not self._expired(cached[0], self._estimate_ttl):
            return cached[1]

        raw = await self._get(f"{_BASE}/route/{route_id}/estimate")
        rows = [_norm_route_estimate_row(r) for r in raw]
        self._estimate_cache[route_id] = (self._clock(), rows)
        return rows

    async def fetch_eta_rows_for_stop(
        self,
        stop_name: str,
        route_names: list[str],
    ) -> list[dict]:
        """ETA rows at stop_name for the given city routes.

        Fetches all route estimates in parallel (no rate limit). Each route's
        estimate is cached and shared with fetch_route_estimate callers.
        Per-route failures are logged and skipped (partial degradation).
        """

        async def _one(name: str) -> list[dict]:
            route_id = await self.get_route_id(name)
            if route_id is None:
                return []

            cached = self._estimate_cache.get(route_id)
            if cached is not None and not self._expired(cached[0], self._estimate_ttl):
                route_rows = cached[1]
            else:
                raw = await self._get(f"{_BASE}/route/{route_id}/estimate")
                route_rows = [_norm_route_estimate_row(r) for r in raw]
                self._estimate_cache[route_id] = (self._clock(), route_rows)

            matching = [r for r in route_rows if stop_name in r.get("stop_name", "")]
            return _dedup_eta_by_min_seq([_route_est_to_eta(r, name) for r in matching])

        tasks = [_one(name) for name in route_names]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        rows: list[dict] = []
        for name, result in zip(route_names, results):
            if isinstance(result, list):
                rows.extend(result)
            else:
                _log.warning("ebus ETA failed for %s: %s", name, result)
        return rows

    async def aclose(self) -> None:
        pass  # shared http client; lifecycle managed by api lifespan
