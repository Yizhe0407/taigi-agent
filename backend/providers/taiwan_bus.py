"""TaiwanBus eBUS provider adapter.

The website exposes JSON-shaped endpoints behind ``.ashx`` handlers, but sends
some responses with an HTML content type.  This adapter treats the response body
as JSON and translates all upstream fields into the provider-neutral contracts in
``providers.bus``.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

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
from providers.ttl_cache import KeyedLocks

_log = logging.getLogger(__name__)

_BASE = "https://www.taiwanbus.tw/eBUSPage"
_LANGUAGE = "C"
_PRIMARY_ROUTE_KEY_SUFFIXES = (1, 2)
_FALLBACK_ROUTE_KEY_SUFFIXES = tuple(range(3, 10))
_ROUTE_CACHE_TTL = 30.0
_ROUTE_INFO_CACHE_TTL = 300.0
_SEARCH_CACHE_TTL = 3600.0
_REQUEST_TIMEOUT = 15.0
_FETCH_CONCURRENCY = 12
_MINUTE_RE = re.compile(r"^(\d+)\s*分")
_TIME_RE = re.compile(r"(\d{1,2}:\d{2})")


@dataclass(frozen=True, slots=True)
class _RouteCandidate:
    route_name: str
    route_no: str
    display_name: str


@dataclass(frozen=True, slots=True)
class _RouteVariant:
    key: str
    suffix: int
    first_stop: str
    last_stop: str
    cars: int
    rows: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class _TimedCache[T]:
    fetched_at: float
    value: T


class TaiwanBusProvider(BusProvider):
    """Provider-neutral adapter for the TaiwanBus public bus site."""

    def __init__(
        self,
        *,
        language: str = _LANGUAGE,
        clock: Any = time.monotonic,
    ) -> None:
        self._language = language
        self._clock = clock
        self._search_cache: dict[str, _TimedCache[list[_RouteCandidate]]] = {}
        self._route_cache: dict[str, _TimedCache[list[RouteStopEstimate]]] = {}
        self._route_info_cache: dict[str, _TimedCache[dict[str, RouteInfo]]] = {}
        self._preferred_candidates: dict[str, _RouteCandidate] = {}
        self._search_locks: KeyedLocks[str] = KeyedLocks()
        self._route_locks: KeyedLocks[str] = KeyedLocks()
        self._route_info_locks: KeyedLocks[str] = KeyedLocks()

    async def _get_json(self, path: str, params: dict[str, object]) -> Any:
        client = get_http_client()
        response = await client.get(
            f"{_BASE}/{path.lstrip('/')}",
            params=params,
            headers={"Accept": "application/json, text/javascript, */*; q=0.01"},
            timeout=_REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        return response.json()

    def _fresh(self, entry: _TimedCache[Any] | None, ttl: float) -> bool:
        return entry is not None and self._clock() - entry.fetched_at < ttl

    async def _search_routes(self, query: str) -> list[_RouteCandidate]:
        cached = self._search_cache.get(query)
        if cached is not None and self._fresh(cached, _SEARCH_CACHE_TTL):
            return cached.value

        async with self._search_locks.acquire(query):
            cached = self._search_cache.get(query)
            if cached is not None and self._fresh(cached, _SEARCH_CACHE_TTL):
                return cached.value
            payload = await self._get_json(
                "Query/ws/getData.ashx",
                {"type": 1, "key": query, "lan": self._language},
            )
            candidates: list[_RouteCandidate] = []
            seen: set[tuple[str, str, str]] = set()
            if isinstance(payload, list):
                for row in payload:
                    if not isinstance(row, dict):
                        continue
                    route_name = str(row.get("srno") or "").strip()
                    route_no = str(row.get("rno") or "").strip()
                    display_name = str(row.get("name") or "").strip()
                    key = (route_name, route_no, display_name)
                    if route_name and route_no and key not in seen:
                        seen.add(key)
                        candidates.append(_RouteCandidate(route_name, route_no, display_name))
            self._search_cache[query] = _TimedCache(self._clock(), candidates)
            return candidates

    async def _fetch_variant(self, candidate: _RouteCandidate, suffix: int) -> _RouteVariant | None:
        key = f"{candidate.route_no}{suffix}"
        payload = await self._get_json("Query/ws/getRData.ashx", {"type": 4, "key": key})
        if not isinstance(payload, dict):
            return None
        raw_rows = payload.get("data")
        if not isinstance(raw_rows, list) or not raw_rows:
            return None
        rows = tuple(row for row in raw_rows if isinstance(row, dict))
        if not rows:
            return None
        first_stop = str(rows[0].get("na") or "").strip()
        last_stop = str(rows[-1].get("na") or "").strip()
        return _RouteVariant(
            key=key,
            suffix=suffix,
            first_stop=first_stop,
            last_stop=last_stop,
            cars=len(payload.get("cars") or []) if isinstance(payload.get("cars"), list) else 0,
            rows=rows,
        )

    async def _fetch_candidates_estimate(
        self,
        route_name: str,
        candidates: list[_RouteCandidate],
    ) -> list[RouteStopEstimate] | None:
        if not candidates:
            return None
        semaphore = asyncio.Semaphore(_FETCH_CONCURRENCY)

        async def fetch(candidate: _RouteCandidate, suffix: int) -> tuple[_RouteVariant | None, bool]:
            async with semaphore:
                try:
                    return await self._fetch_variant(candidate, suffix), False
                except Exception as exc:
                    _log.warning("TaiwanBus route variant failed (%s/%s): %s", route_name, suffix, exc)
                    return None, True

        async def fetch_suffixes(suffixes: tuple[int, ...]) -> list[tuple[_RouteVariant | None, bool]]:
            return await asyncio.gather(*(fetch(candidate, suffix) for candidate in candidates for suffix in suffixes))

        fetched = await fetch_suffixes(_PRIMARY_ROUTE_KEY_SUFFIXES)
        if fetched and all(failed and variant is None for variant, failed in fetched):
            raise RuntimeError(f"TaiwanBus route estimate unavailable: {route_name}")
        variants = [variant for variant, _failed in fetched if variant is not None]
        if not variants:
            fallback_fetched = await fetch_suffixes(_FALLBACK_ROUTE_KEY_SUFFIXES)
            if fallback_fetched and all(failed and variant is None for variant, failed in fallback_fetched):
                raise RuntimeError(f"TaiwanBus route estimate unavailable: {route_name}")
            variants = [variant for variant, _failed in fallback_fetched if variant is not None]
        if not variants:
            return None

        groups: dict[tuple[str, str], list[_RouteVariant]] = {}
        for variant in variants:
            groups.setdefault((variant.first_stop, variant.last_stop), []).append(variant)
        ordered_groups = sorted(groups.values(), key=lambda group: min(v.suffix for v in group))

        result: list[RouteStopEstimate] = []
        for index, group in enumerate(ordered_groups):
            direction = Direction.OUTBOUND if index == 0 else Direction.INBOUND
            variant = min(group, key=lambda item: (0 if item.cars else 1, item.suffix))
            for row in variant.rows:
                result.append(self._normalize_stop(row, route_name, direction))
        return result

    async def _fetch_route_estimate_uncached(self, route_name: str) -> list[RouteStopEstimate] | None:
        preferred = self._preferred_candidates.get(route_name)
        if preferred is not None:
            return await self._fetch_candidates_estimate(route_name, [preferred])
        candidates = [candidate for candidate in await self._search_routes(route_name) if candidate.route_name == route_name]
        # Route numbers are not globally unique.  The stop-scoped lookup path
        # below records the exact candidate; a direct lookup without that
        # context uses the first exact result instead of merging unrelated
        # routes from other counties.
        return await self._fetch_candidates_estimate(route_name, candidates[:1])

    @staticmethod
    def _normalize_stop(row: dict[str, Any], route_name: str, direction: Direction) -> RouteStopEstimate:
        status, eta_seconds, scheduled_time = _parse_ptime(row.get("ptime"))
        sequence = _as_int(row.get("sequence", row.get("idx")))
        return RouteStopEstimate(
            stop_name=str(row.get("na") or "").strip(),
            sequence=sequence,
            direction=direction,
            status=status,
            eta_seconds=eta_seconds,
            scheduled_time=scheduled_time,
            vehicle_id=str(row.get("car") or row.get("obu_id") or "") or None,
            route_name=route_name,
        )

    async def fetch_route_estimate(self, route_name: str) -> list[RouteStopEstimate] | None:
        cached = self._route_cache.get(route_name)
        if cached is not None and self._fresh(cached, _ROUTE_CACHE_TTL):
            return cached.value
        async with self._route_locks.acquire(route_name):
            cached = self._route_cache.get(route_name)
            if cached is not None and self._fresh(cached, _ROUTE_CACHE_TTL):
                return cached.value
            rows = await self._fetch_route_estimate_uncached(route_name)
            if rows is not None:
                self._route_cache[route_name] = _TimedCache(self._clock(), rows)
            return rows

    async def load_route_info(self, stop_name: str) -> dict[str, RouteInfo]:
        cached = self._route_info_cache.get(stop_name)
        if cached is not None and self._fresh(cached, _ROUTE_INFO_CACHE_TTL):
            return cached.value
        async with self._route_info_locks.acquire(stop_name):
            cached = self._route_info_cache.get(stop_name)
            if cached is not None and self._fresh(cached, _ROUTE_INFO_CACHE_TTL):
                return cached.value
            candidates = await self._search_routes(stop_name)
            unique_candidates = list({(candidate.route_name, candidate.route_no): candidate for candidate in candidates}.values())
            semaphore = asyncio.Semaphore(4)

            async def one(candidate: _RouteCandidate) -> tuple[_RouteCandidate, list[RouteStopEstimate] | None]:
                async with semaphore:
                    return candidate, await self._fetch_candidates_estimate(candidate.route_name, [candidate])

            results = await asyncio.gather(*(one(candidate) for candidate in unique_candidates))
            info: dict[str, RouteInfo] = {}
            for candidate, rows in results:
                if candidate.route_name in info or not rows or not any(_same_stop(stop_name, row.stop_name) for row in rows):
                    continue
                self._preferred_candidates[candidate.route_name] = candidate
                self._route_cache[candidate.route_name] = _TimedCache(self._clock(), rows)
                info[candidate.route_name] = _route_info_from_rows(candidate.route_name, rows)
            self._route_info_cache[stop_name] = _TimedCache(self._clock(), info)
            return info

    async def fetch_routes_at_stop(self, stop_name: str) -> list[RouteAtStop]:
        info = await self.load_route_info(stop_name)
        return [RouteAtStop(route_name=route_name, direction=direction) for route_name in info for direction in (Direction.OUTBOUND, Direction.INBOUND)]

    async def fetch_eta_at_stop(self, stop_name: str) -> list[StopArrival] | None:
        info = await self.load_route_info(stop_name)
        if not info:
            return None if await self._search_routes(stop_name) else []
        result: list[StopArrival] = []
        for route_name in info:
            rows = await self.fetch_route_estimate(route_name)
            if not rows:
                continue
            matching = [row for row in rows if _same_stop(stop_name, row.stop_name)]
            best_by_direction: dict[Direction, RouteStopEstimate] = {}
            for row in matching:
                current = best_by_direction.get(row.direction)
                if current is None or _arrival_sort_key(row) < _arrival_sort_key(current):
                    best_by_direction[row.direction] = row
            result.extend(
                StopArrival(
                    route_name=route_name,
                    direction=direction,
                    status=row.status,
                    eta_seconds=row.eta_seconds,
                    sequence=row.sequence,
                    scheduled_time=row.scheduled_time,
                    vehicle_id=row.vehicle_id,
                )
                for direction, row in best_by_direction.items()
            )
        return result


def _as_int(value: object) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _same_stop(query: str, actual: str) -> bool:
    query = query.strip()
    actual = actual.strip()
    return bool(query and actual and (query == actual or query in actual or actual in query))


def _route_info_from_rows(route_name: str, rows: Iterable[RouteStopEstimate]) -> RouteInfo:
    terminals: dict[Direction, tuple[int, str]] = {}
    for row in rows:
        sequence = row.sequence or 0
        current = terminals.get(row.direction)
        if row.stop_name and (current is None or sequence > current[0]):
            terminals[row.direction] = (sequence, row.stop_name)
    return RouteInfo(
        route_name=route_name,
        outbound_destination=terminals.get(Direction.OUTBOUND, (0, ""))[1],
        inbound_destination=terminals.get(Direction.INBOUND, (0, ""))[1],
    )


def _arrival_sort_key(row: RouteStopEstimate) -> tuple[int, int]:
    if row.status is StopStatus.AVAILABLE and row.eta_seconds is not None:
        return (0, row.eta_seconds)
    if row.status is StopStatus.NOT_DEPARTED:
        return (1, row.sequence or 9999)
    if row.status is StopStatus.LAST_DEPARTED:
        return (3, 9999)
    return (2, row.sequence or 9999)


def _parse_ptime(value: object) -> tuple[StopStatus, int | None, str | None]:
    text = str(value or "").strip()
    if not text:
        return StopStatus.UNKNOWN, None, None
    if "末班" in text or "駛離" in text:
        return StopStatus.LAST_DEPARTED, None, None
    if "進站" in text or "到站" in text:
        return StopStatus.AVAILABLE, 0, None
    minute_match = _MINUTE_RE.match(text)
    if minute_match:
        return StopStatus.AVAILABLE, int(minute_match.group(1)) * 60, None
    time_match = _TIME_RE.search(text)
    if time_match:
        return StopStatus.NOT_DEPARTED, None, time_match.group(1)
    if "發車" in text or "動態" in text:
        return StopStatus.NOT_DEPARTED, None, None
    return StopStatus.UNKNOWN, None, None
