"""TDX (tdx.transportdata.tw) concrete `BusProvider` implementation.

Covers both YunlinCounty city buses and intercity (公路客運) buses.

## Circular-route correctness

TDX returns one EstimatedTimeOfArrival row per stop *occurrence*.  Circular
routes (e.g., Y02: 斗六火車站 seq=1 → … → 斗六火車站 seq=10) produce two rows
for the same stop name — one for the boarding point (seq=1) and one for the
arriving bus completing the loop (seq=10).

To avoid showing the irrelevant arrival row, `load_route_info` extracts the
StopUID of the *first* occurrence of the kiosk stop in each route direction
(the boarding point) from the `StopOfRoute` payload.  Subsequent calls to
`fetch_eta_at_stop` query TDX using those UIDs instead of the stop name, so
TDX only returns rows for the boarding-point stops.

The UID set is cached alongside route_info.  A name-based fallback with
min-sequence dedup handles the cold-start window where `load_route_info` and
`fetch_eta_at_stop` are first called concurrently.

## Route classification

  Intercity: sub_route_name matches ^7\\d{3} (7000D, 7120, 7123A, …)
  City:      everything else (101, 201, 701, Y01, …)

`fetch_route_estimate` only queries the relevant endpoint to halve request
volume and avoid 429 rate limits.

## Internal row schema

  fetch_eta_at_stop rows:
    sub_route_name  str
    direction       int    0=去程 1=回程  (TDX Direction)
    stop_status     int    0=正常 1=未發車 2=不停 3=末班過 4=今日停駛
    estimate_seconds int|None
    stop_sequence   int|None

  fetch_route_estimate rows:
    stop_name       str
    stop_sequence   int|None
    direction       int
    stop_status     int
    estimate_seconds int|None

  load_route_info → {sub_route_name: {"id": str, "go_dest": str, "back_dest": str}}
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable

import httpx

from providers.bus import BusProvider
from providers.http import get_http_client
from providers.tdx_auth import TdxTokenClient
from providers.ttl_cache import KeyedLocks, TtlCache
from telemetry import get_telemetry

_log = logging.getLogger(__name__)

_BASE = "https://tdx.transportdata.tw/api/basic/v2/Bus"
_CITY = "YunlinCounty"

_DEFAULT_ROUTE_INFO_TTL = 600.0
_ROUTE_INFO_PARTIAL_TTL = 60.0  # short TTL when a StopOfRoute endpoint failed mid-fetch
_DEFAULT_ROUTE_ESTIMATE_TTL = 30.0  # TDX updates ~30 s; 10 s caused 429 rate-limit hits
_MAX_ROUTE_ESTIMATE_CACHE_ENTRIES = 256
_MIN_ROUTE_INFO_SWEEP = 32  # don't bother sweeping _route_info_by_stop below this size
_DEFAULT_ETA_TTL = 60.0  # must exceed wait_for(12s) + warmup_sleep(25s) = 37s to break 429 cascade
_MAX_RETRIES = 1  # retries on HTTP 429; Retry-After is 20-40 s so 3 retries = 60-120 s blocking
_MAX_UIDS_PER_FILTER = 15  # keeps encoded $filter well under typical gateway URL-length limits
_MAX_STALE_SECONDS = 300.0  # upstream-down grace period; beyond this, stop serving stale data
_ETA_FETCH_TIMEOUT = 12.0  # outer budget for fetch_eta_at_stop's asyncio.wait_for
_REQUEST_TIMEOUT_SECONDS = 10.0  # per-request timeout; must stay < _ETA_FETCH_TIMEOUT or the
# wait_for cancels the request before its own timeout can ever fire

_INTERCITY_RE = re.compile(r"^7\d{3}")


def _is_intercity(sub_route_name: str) -> bool:
    """True for public highway buses (7000D, 7120, 7123A, …), False for city buses."""
    return bool(_INTERCITY_RE.match(sub_route_name))


def _odata_escape(value: str) -> str:
    """Escape a single-quoted OData string literal (doubles embedded quotes).

    stop_name / route_name ultimately trace back to LLM tool-call arguments;
    an unescaped `'` breaks the `$filter` expression and turns into a 500
    instead of a clean "not found".
    """
    return value.replace("'", "''")


def _zh(field: object) -> str:
    """Extract Zh_tw from a TDX localised name dict {"Zh_tw": ..., "En": ...}."""
    if isinstance(field, dict):
        return str(field.get("Zh_tw") or "").strip()
    return ""


def _safe_list(result: object) -> list[dict]:
    """Validate a gathered endpoint result and degrade malformed data to empty."""
    if isinstance(result, BaseException):
        _log.warning("TDX endpoint error (degraded gracefully): %s", result)
        return []
    if not isinstance(result, list):
        _log.warning("TDX endpoint returned a non-list payload")
        return []
    return [item for item in result if isinstance(item, dict)]


class TdxBusProvider(BusProvider):
    """HTTP-backed `BusProvider` for tdx.transportdata.tw."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        route_info_ttl_seconds: float | None = _DEFAULT_ROUTE_INFO_TTL,
        route_estimate_ttl_seconds: float | None = _DEFAULT_ROUTE_ESTIMATE_TTL,
        eta_ttl_seconds: float | None = _DEFAULT_ETA_TTL,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._route_info_ttl = route_info_ttl_seconds
        self._route_estimate_ttl = route_estimate_ttl_seconds
        self._eta_ttl = eta_ttl_seconds
        self._clock = clock
        self._sleep = sleep
        # `get_http_client` below binds the name as currently seen in this module's
        # globals, so tests that monkeypatch `tdx_bus.get_http_client` before
        # constructing the provider also cover the token client's requests.
        self._token_client = TdxTokenClient(
            client_id,
            client_secret,
            clock=clock,
            timeout=_REQUEST_TIMEOUT_SECONDS,
            http_client_factory=get_http_client,
        )
        # stop_name → (fetched_at, route_info_dict, had_failures)
        # had_failures=True uses _ROUTE_INFO_PARTIAL_TTL so a StopOfRoute endpoint
        # that failed mid-fetch gets re-checked soon instead of caching a partial
        # result for the full TTL.
        self._route_info_by_stop: dict[str, tuple[float, dict[str, dict], bool]] = {}
        # per-key lock prevents concurrent cache-miss storms on load_route_info;
        # KeyedLocks drops a key once no task holds or awaits it, so the registry
        # is bounded by in-flight concurrency instead of by every stop ever asked for.
        self._route_info_locks: KeyedLocks[str] = KeyedLocks()
        # Amortised sweep bookkeeping for _route_info_by_stop (see _maybe_sweep_route_info).
        self._route_info_sweep_threshold = _MIN_ROUTE_INFO_SWEEP
        # stop_name → set of boarding StopUIDs (first occurrence of that stop in each route)
        self._kiosk_uids: dict[str, set[str]] = {}
        # sub_route_name → (fetched_at, rows)
        self._route_estimate_cache: OrderedDict[str, tuple[float, list[dict]]] = OrderedDict()
        self._route_estimate_ttl_cache: TtlCache[str, list[dict]] = TtlCache(
            self._route_estimate_cache,
            clock=clock,
            cache_name="TDX route estimate",
            record_hit=lambda hit: get_telemetry().record_cache_lookup(cache="tdx.route_estimate", hit=hit),
        )
        # stop_name → (fetched_at, rows)
        self._eta_cache: dict[str, tuple[float, list[dict]]] = {}
        self._eta_ttl_cache: TtlCache[str, list[dict]] = TtlCache(
            self._eta_cache,
            clock=clock,
            cache_name="TDX ETA",
            record_hit=lambda hit: get_telemetry().record_cache_lookup(cache="tdx.eta", hit=hit),
        )

    @staticmethod
    def _retry_after_seconds(resp: object, default: float) -> float:
        """Parse Retry-After as seconds; RFC also allows an HTTP-date, which
        float() can't handle — fall back to `default` instead of raising."""
        raw = resp.headers.get("Retry-After", "")  # type: ignore[attr-defined]
        try:
            return float(raw)
        except ValueError:
            return default

    async def _get(self, url: str, params: dict) -> list[dict]:
        http = get_http_client()
        attempt = 0
        while True:

            async def _do(token: str, url: str = url, params: dict = params) -> httpx.Response:
                return await http.get(
                    url,
                    params={**params, "$format": "JSON"},
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=_REQUEST_TIMEOUT_SECONDS,
                )

            # Handles the 401 (token revoked/expired early server-side) → force
            # refresh → retry-once dance internally; independent of the 429 retry
            # budget below — a 401 refresh must not consume a rate-limit attempt.
            resp = await self._token_client.request_with_retry(_do)
            if resp.status_code == 429 and attempt < _MAX_RETRIES:
                wait = self._retry_after_seconds(resp, float(1 << attempt))
                _log.warning("TDX 429 on %s; retry in %.0fs (attempt %d/%d)", url, wait, attempt + 1, _MAX_RETRIES)
                await self._sleep(wait)
                attempt += 1
                continue
            resp.raise_for_status()
            payload = resp.json()
            if not isinstance(payload, list):
                raise ValueError("TDX endpoint returned a non-list payload")
            return [item for item in payload if isinstance(item, dict)]

    # ── Normalizers ───────────────────────────────────────────────────────────

    @staticmethod
    def _norm_eta(row: dict) -> dict:
        return {
            "sub_route_name": _zh(row.get("SubRouteName")),
            "direction": row.get("Direction", 0),
            "stop_status": row.get("StopStatus", 1),
            "estimate_seconds": row.get("EstimateTime"),
            "stop_sequence": row.get("StopSequence"),
        }

    @staticmethod
    def _norm_stop_eta(row: dict) -> dict:
        return {
            "stop_name": _zh(row.get("StopName")),
            "stop_sequence": row.get("StopSequence"),
            "direction": row.get("Direction", 0),
            "stop_status": row.get("StopStatus", 1),
            "estimate_seconds": row.get("EstimateTime"),
        }

    # ── BusProvider ───────────────────────────────────────────────────────────

    async def fetch_routes_at_stop(self, stop_name: str) -> list[dict]:
        """Unique subroutes at `stop_name` (city + intercity)."""
        city, intercity, _had_failures = await self._stop_of_route(stop_name)
        seen: set[str] = set()
        result: list[dict] = []
        for rec in city + intercity:
            name = _zh(rec.get("SubRouteName"))
            if name and name not in seen:
                seen.add(name)
                result.append({"sub_route_name": name, "direction": rec.get("Direction", 0)})
        return result

    async def fetch_eta_at_stop(self, stop_name: str) -> list[dict]:
        """ETA rows for every subroute at `stop_name`.

        Uses StopUID filtering when boarding UIDs are already cached from a
        prior `load_route_info` call.  Falls back to stop-name filtering with
        min-sequence dedup during the cold-start window when both methods are
        called concurrently for the first time.
        """

        async def _fetch() -> list[dict]:
            uids = self._kiosk_uids.get(stop_name)
            fetch = self._fetch_eta_by_uids(uids) if uids else self._fetch_eta_by_name(stop_name)
            return await asyncio.wait_for(fetch, timeout=_ETA_FETCH_TIMEOUT)

        # Stale-serve keeps fetched_at unbumped on failure, so staleness keeps
        # accumulating toward _MAX_STALE_SECONDS instead of resetting on every
        # failed retry (which would keep an upstream outage's last-good data alive
        # forever) — see TtlCache.get_or_fetch.
        return await self._eta_ttl_cache.get_or_fetch(
            stop_name,
            _fetch,
            ttl=self._eta_ttl,
            stale_ttl=self._stale_ttl(self._eta_ttl),
        )

    async def fetch_route_estimate(self, sub_route_name: str) -> list[dict]:
        async def _fetch() -> list[dict]:
            # Only query the endpoint that owns this route — halves request volume.
            if _is_intercity(sub_route_name):
                raw = await self._get(
                    f"{_BASE}/EstimatedTimeOfArrival/InterCity",
                    {"$filter": f"SubRouteName/Zh_tw eq '{_odata_escape(sub_route_name)}'"},
                )
            else:
                raw = await self._get(
                    f"{_BASE}/EstimatedTimeOfArrival/City/{_CITY}",
                    {"$filter": f"SubRouteName/Zh_tw eq '{_odata_escape(sub_route_name)}'"},
                )
            return [self._norm_stop_eta(r) for r in raw]

        def _touch(key: str) -> None:
            self._route_estimate_cache.move_to_end(key)

        def _store(key: str, _rows: list[dict]) -> None:
            self._route_estimate_cache.move_to_end(key)
            while len(self._route_estimate_cache) > _MAX_ROUTE_ESTIMATE_CACHE_ENTRIES:
                self._route_estimate_cache.popitem(last=False)

        return await self._route_estimate_ttl_cache.get_or_fetch(
            sub_route_name,
            _fetch,
            ttl=self._route_estimate_ttl,
            stale_ttl=self._stale_ttl(self._route_estimate_ttl),
            on_hit=_touch,
            on_store=_store,
        )

    async def load_route_info(self, stop_name: str) -> dict[str, dict]:
        cached = self._route_info_by_stop.get(stop_name)
        ttl = self._route_info_ttl if cached is None or not cached[2] else _ROUTE_INFO_PARTIAL_TTL
        hit = cached is not None and not self._expired(cached[0], ttl)
        get_telemetry().record_cache_lookup(cache="tdx.route_info", hit=hit)
        if cached is not None and hit:
            return cached[1]

        # Not a TtlCache: the cached value is a 3-tuple (info, had_failures) whose
        # TTL depends on had_failures from the *previous* fetch, which doesn't fit
        # TtlCache's fixed-ttl/2-tuple shape without contorting it more than the
        # manual per-key lock below costs.
        async with self._route_info_locks.acquire(stop_name):
            # Re-check after acquiring: first caller fills cache, subsequent callers hit it.
            cached = self._route_info_by_stop.get(stop_name)
            ttl = self._route_info_ttl if cached is None or not cached[2] else _ROUTE_INFO_PARTIAL_TTL
            if cached is not None and not self._expired(cached[0], ttl):
                return cached[1]

            city, intercity, had_failures = await self._stop_of_route(stop_name)
            info, boarding_uids = self._build_route_info(city + intercity, stop_name)
            self._route_info_by_stop[stop_name] = (self._clock(), info, had_failures)
            if boarding_uids:
                self._kiosk_uids[stop_name] = boarding_uids
            self._maybe_sweep_route_info()
            return info

    def _maybe_sweep_route_info(self) -> None:
        """Evict expired `_route_info_by_stop` entries (and their `_kiosk_uids`).

        Both dicts only ever expired at read time, so every stop name ever looked
        up stayed resident for the life of the process.  Sweeping on store bounds
        them to "distinct stops seen within one route-info TTL".

        Retention uses the full TTL even for `had_failures` entries whose read-time
        TTL is the shorter partial one: keeping a dead entry slightly longer is
        harmless, dropping a live one would cost an extra upstream fetch.

        `_kiosk_uids` carries no timestamp of its own, so it is pruned strictly in
        lockstep with the route-info entries actually removed here — never by
        reconciling the whole dict, which would also discard directly-seeded UIDs.
        """
        if self._route_info_ttl is None or self._route_info_ttl <= 0:
            return
        if len(self._route_info_by_stop) <= self._route_info_sweep_threshold:
            return
        expired = [key for key, (fetched_at, _info, _failed) in self._route_info_by_stop.items() if self._expired(fetched_at, self._route_info_ttl)]
        for key in expired:
            del self._route_info_by_stop[key]
            self._kiosk_uids.pop(key, None)
        self._route_info_sweep_threshold = max(_MIN_ROUTE_INFO_SWEEP, 2 * len(self._route_info_by_stop))

    async def load_route_terminals(self, route_name: str) -> dict[str, str]:
        """Return {go_dest, back_dest} from TDX StopOfRoute filtered by route name.

        Used to supplement ebus data when ebus encodes rare CJK characters as '?'.
        """
        filter_expr = f"SubRouteName/Zh_tw eq '{_odata_escape(route_name)}'"
        results = await asyncio.gather(
            self._get(f"{_BASE}/StopOfRoute/City/{_CITY}", {"$filter": filter_expr}),
            self._get(f"{_BASE}/StopOfRoute/InterCity", {"$filter": filter_expr}),
            return_exceptions=True,
        )
        records = _safe_list(results[0]) + _safe_list(results[1])
        terminals: dict[int, str] = {}
        for rec in records:
            direction = rec.get("Direction", 0)
            stops = rec.get("Stops") or []
            if not stops:
                continue
            ordered = sorted(stops, key=lambda s: s.get("StopSequence", 0))
            terminal = _zh(ordered[-1].get("StopName"))
            if terminal and "?" not in terminal:
                terminals[direction] = terminal
        return {
            "go_dest": terminals.get(0, ""),
            "back_dest": terminals.get(1, ""),
        }

    async def aclose(self) -> None:
        pass  # shared http client; lifecycle managed by api lifespan

    # ── ETA fetch helpers ──────────────────────────────────────────────────────

    async def _fetch_eta_by_uids(self, uids: set[str]) -> list[dict]:
        """Query by StopUID — precise, no dedup needed.

        Chunks the OR filter so an interchange stop with many boarding UIDs
        (e.g. 高鐵雲林站, served by both city and intercity routes in both
        directions) can't build a single `$filter` clause long enough to hit
        a gateway/proxy URL-length limit.
        """
        uid_list = sorted(uids)
        chunks = [uid_list[i : i + _MAX_UIDS_PER_FILTER] for i in range(0, len(uid_list), _MAX_UIDS_PER_FILTER)]

        async def _fetch_chunk(chunk: list[str]) -> tuple[object, object]:
            uid_filter = " or ".join(f"StopUID eq '{uid}'" for uid in chunk)
            return await asyncio.gather(
                self._get(f"{_BASE}/EstimatedTimeOfArrival/City/{_CITY}", {"$filter": uid_filter}),
                self._get(f"{_BASE}/EstimatedTimeOfArrival/InterCity", {"$filter": uid_filter}),
                return_exceptions=True,
            )

        chunk_results = await asyncio.gather(*[_fetch_chunk(chunk) for chunk in chunks])
        all_results = [r for pair in chunk_results for r in pair]
        # Only raise (→ caller serves stale cache) when every request across every
        # chunk failed; a partial outage should still surface the rows that succeeded.
        if all_results and all(isinstance(r, BaseException) for r in all_results):
            raise all_results[0]  # type: ignore[misc]
        rows = [row for r in all_results for row in _safe_list(r)]
        return [self._norm_eta(r) for r in rows]

    async def _fetch_eta_by_name(self, stop_name: str) -> list[dict]:
        """Fallback: query by stop name and dedup by min sequence."""
        results = await asyncio.gather(
            self._get(
                f"{_BASE}/EstimatedTimeOfArrival/City/{_CITY}",
                {"$filter": f"StopName/Zh_tw eq '{_odata_escape(stop_name)}'"},
            ),
            self._get(
                f"{_BASE}/EstimatedTimeOfArrival/InterCity",
                {"$filter": f"StopName/Zh_tw eq '{_odata_escape(stop_name)}'"},
            ),
            return_exceptions=True,
        )
        city_rows = _safe_list(results[0])
        intercity_rows = _safe_list(results[1])
        if isinstance(results[0], BaseException) and isinstance(results[1], BaseException):
            raise results[0]
        all_rows = [self._norm_eta(r) for r in city_rows + intercity_rows]
        return self._dedup_by_min_sequence(all_rows)

    # ── Internal ──────────────────────────────────────────────────────────────

    async def _stop_of_route(self, stop_name: str) -> tuple[list[dict], list[dict], bool]:
        """Fetch StopOfRoute for City and InterCity; degrade gracefully on error.

        Returns (city_rows, intercity_rows, had_failures) — had_failures is True
        when either endpoint errored, so callers can cache the (possibly
        incomplete) result with a short TTL instead of the full route-info TTL.
        """
        results = await asyncio.gather(
            self._get(
                f"{_BASE}/StopOfRoute/City/{_CITY}",
                {"$filter": f"Stops/any(s: s/StopName/Zh_tw eq '{_odata_escape(stop_name)}')"},
            ),
            self._get(
                f"{_BASE}/StopOfRoute/InterCity",
                {"$filter": f"Stops/any(s: s/StopName/Zh_tw eq '{_odata_escape(stop_name)}')"},
            ),
            return_exceptions=True,
        )
        had_failures = any(isinstance(r, BaseException) for r in results)
        return _safe_list(results[0]), _safe_list(results[1]), had_failures

    @staticmethod
    def _build_route_info(records: list[dict], kiosk_stop: str) -> tuple[dict[str, dict], set[str]]:
        """Build route_info and collect boarding StopUIDs.

        For each (subroute, direction), the *first* stop occurrence of
        `kiosk_stop` in the ordered stop list is the boarding point.  Its
        StopUID is added to `boarding_uids` so that future ETA queries can
        use UID filtering instead of name filtering, avoiding circular-route
        duplicate rows.

        Returns (route_info, boarding_uids).
        """
        terminals: dict[tuple[str, int], str] = {}
        boarding_uids: set[str] = set()

        for rec in records:
            name = _zh(rec.get("SubRouteName"))
            if not name:
                continue
            direction = rec.get("Direction", 0)
            stops = rec.get("Stops") or []
            if not stops:
                continue
            ordered = sorted(stops, key=lambda s: s.get("StopSequence", 0))

            # Last stop = terminal for direction label
            terminal = _zh(ordered[-1].get("StopName"))
            if terminal:
                terminals[(name, direction)] = terminal

            # First occurrence of kiosk stop = boarding point → collect its UID
            for stop in ordered:
                if _zh(stop.get("StopName")) == kiosk_stop:
                    uid = stop.get("StopUID")
                    if uid:
                        boarding_uids.add(uid)
                    break  # only the first occurrence matters

        all_names = {name for name, _ in terminals}
        route_info = {
            name: {
                "id": name,
                "go_dest": terminals.get((name, 0), ""),
                "back_dest": terminals.get((name, 1), ""),
            }
            for name in all_names
        }
        return route_info, boarding_uids

    @staticmethod
    def _dedup_by_min_sequence(rows: list[dict]) -> list[dict]:
        """Fallback dedup: keep one row per (sub_route_name, direction) by min StopSequence."""
        best: dict[tuple[str, int], dict] = {}
        for row in rows:
            key = (row.get("sub_route_name", ""), row.get("direction", 0))
            seq = row.get("stop_sequence") or 9999
            existing = best.get(key)
            if existing is None or (existing.get("stop_sequence") or 9999) > seq:
                best[key] = row
        return list(best.values())

    def _expired(self, fetched_at: float, ttl: float | None) -> bool:
        if ttl is None or ttl <= 0:
            return False
        return (self._clock() - fetched_at) >= ttl

    @staticmethod
    def _stale_ttl(ttl: float | None) -> float | None:
        return None if ttl is None else ttl + _MAX_STALE_SECONDS
