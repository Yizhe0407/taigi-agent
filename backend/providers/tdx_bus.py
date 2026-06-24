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
from collections.abc import Awaitable, Callable

from providers.bus import BusProvider
from providers.http import get_http_client
from telemetry import get_telemetry

_log = logging.getLogger(__name__)

_TOKEN_URL = "https://tdx.transportdata.tw/auth/realms/TDXConnect/protocol/openid-connect/token"
_BASE = "https://tdx.transportdata.tw/api/basic/v2/Bus"
_CITY = "YunlinCounty"
_TOKEN_BUFFER_SECONDS = 60.0

_DEFAULT_ROUTE_INFO_TTL = 600.0
_DEFAULT_ROUTE_ESTIMATE_TTL = 30.0  # TDX updates ~30 s; 10 s caused 429 rate-limit hits
_MAX_RETRIES = 3  # retries on HTTP 429; backoff 1→2→4 s (or Retry-After header)

_INTERCITY_RE = re.compile(r"^7\d{3}")


def _is_intercity(sub_route_name: str) -> bool:
    """True for public highway buses (7000D, 7120, 7123A, …), False for city buses."""
    return bool(_INTERCITY_RE.match(sub_route_name))


def _zh(field: object) -> str:
    """Extract Zh_tw from a TDX localised name dict {"Zh_tw": ..., "En": ...}."""
    if isinstance(field, dict):
        return str(field.get("Zh_tw") or "").strip()
    return ""


def _safe_list(result: list[dict] | BaseException) -> list[dict]:
    """Return an empty list when an asyncio.gather result is an exception."""
    if isinstance(result, BaseException):
        _log.warning("TDX endpoint error (degraded gracefully): %s", result)
        return []
    return result


class TdxBusProvider(BusProvider):
    """HTTP-backed `BusProvider` for tdx.transportdata.tw."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        route_info_ttl_seconds: float | None = _DEFAULT_ROUTE_INFO_TTL,
        route_estimate_ttl_seconds: float | None = _DEFAULT_ROUTE_ESTIMATE_TTL,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._route_info_ttl = route_info_ttl_seconds
        self._route_estimate_ttl = route_estimate_ttl_seconds
        self._clock = clock
        self._sleep = sleep
        # token cache
        self._token: str = ""
        self._token_expires_at: float = 0.0
        # stop_name → (fetched_at, route_info_dict)
        self._route_info_by_stop: dict[str, tuple[float, dict[str, dict]]] = {}
        # stop_name → set of boarding StopUIDs (first occurrence of that stop in each route)
        self._kiosk_uids: dict[str, set[str]] = {}
        # sub_route_name → (fetched_at, rows)
        self._route_estimate_cache: dict[str, tuple[float, list[dict]]] = {}

    # ── Auth ──────────────────────────────────────────────────────────────────

    async def _get_token(self) -> str:
        if self._clock() < self._token_expires_at:
            return self._token
        http = get_http_client()
        resp = await http.post(
            _TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            },
            timeout=15.0,
        )
        resp.raise_for_status()
        body = resp.json()
        self._token = body["access_token"]
        expires_in = float(body.get("expires_in", 3600))
        self._token_expires_at = self._clock() + expires_in - _TOKEN_BUFFER_SECONDS
        return self._token

    async def _get(self, url: str, params: dict) -> list[dict]:
        token = await self._get_token()
        http = get_http_client()
        for attempt in range(_MAX_RETRIES + 1):
            resp = await http.get(
                url,
                params={**params, "$format": "JSON"},
                headers={"Authorization": f"Bearer {token}"},
                timeout=15.0,
            )
            if resp.status_code == 429 and attempt < _MAX_RETRIES:
                wait = float(resp.headers.get("Retry-After", 1 << attempt))
                _log.warning("TDX 429 on %s; retry in %.0fs (attempt %d/%d)", url, wait, attempt + 1, _MAX_RETRIES)
                await self._sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()
        raise RuntimeError("unreachable")

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
        city, intercity = await self._stop_of_route(stop_name)
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
        uids = self._kiosk_uids.get(stop_name)
        if uids:
            return await self._fetch_eta_by_uids(uids)
        return await self._fetch_eta_by_name(stop_name)

    async def fetch_route_estimate(self, sub_route_name: str) -> list[dict]:
        cached = self._route_estimate_cache.get(sub_route_name)
        hit = cached is not None and not self._expired(cached[0], self._route_estimate_ttl)
        get_telemetry().record_cache_lookup(cache="tdx.route_estimate", hit=hit)
        if hit:
            return cached[1]

        # Only query the endpoint that owns this route — halves request volume.
        if _is_intercity(sub_route_name):
            raw = await self._get(
                f"{_BASE}/EstimatedTimeOfArrival/InterCity",
                {"$filter": f"SubRouteName/Zh_tw eq '{sub_route_name}'"},
            )
        else:
            raw = await self._get(
                f"{_BASE}/EstimatedTimeOfArrival/City/{_CITY}",
                {"$filter": f"SubRouteName/Zh_tw eq '{sub_route_name}'"},
            )

        rows = [self._norm_stop_eta(r) for r in raw]
        self._route_estimate_cache[sub_route_name] = (self._clock(), rows)
        return rows

    async def load_route_info(self, stop_name: str) -> dict[str, dict]:
        cached = self._route_info_by_stop.get(stop_name)
        hit = cached is not None and not self._expired(cached[0], self._route_info_ttl)
        get_telemetry().record_cache_lookup(cache="tdx.route_info", hit=hit)
        if hit:
            return cached[1]

        city, intercity = await self._stop_of_route(stop_name)
        info, boarding_uids = self._build_route_info(city + intercity, stop_name)
        self._route_info_by_stop[stop_name] = (self._clock(), info)
        if boarding_uids:
            self._kiosk_uids[stop_name] = boarding_uids
        return info

    async def aclose(self) -> None:
        pass  # shared http client; lifecycle managed by api lifespan

    # ── ETA fetch helpers ──────────────────────────────────────────────────────

    async def _fetch_eta_by_uids(self, uids: set[str]) -> list[dict]:
        """Query by StopUID — precise, no dedup needed."""
        uid_filter = " or ".join(f"StopUID eq '{uid}'" for uid in uids)
        results = await asyncio.gather(
            self._get(f"{_BASE}/EstimatedTimeOfArrival/City/{_CITY}", {"$filter": uid_filter}),
            self._get(f"{_BASE}/EstimatedTimeOfArrival/InterCity", {"$filter": uid_filter}),
            return_exceptions=True,
        )
        city_rows = _safe_list(results[0])
        intercity_rows = _safe_list(results[1])
        if isinstance(results[0], BaseException) and isinstance(results[1], BaseException):
            raise results[0]
        return [self._norm_eta(r) for r in city_rows + intercity_rows]

    async def _fetch_eta_by_name(self, stop_name: str) -> list[dict]:
        """Fallback: query by stop name and dedup by min sequence."""
        results = await asyncio.gather(
            self._get(
                f"{_BASE}/EstimatedTimeOfArrival/City/{_CITY}",
                {"$filter": f"StopName/Zh_tw eq '{stop_name}'"},
            ),
            self._get(
                f"{_BASE}/EstimatedTimeOfArrival/InterCity",
                {"$filter": f"StopName/Zh_tw eq '{stop_name}'"},
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

    async def _stop_of_route(self, stop_name: str) -> tuple[list[dict], list[dict]]:
        """Fetch StopOfRoute for City and InterCity; degrade gracefully on error."""
        results = await asyncio.gather(
            self._get(
                f"{_BASE}/StopOfRoute/City/{_CITY}",
                {"$filter": f"Stops/any(s: s/StopName/Zh_tw eq '{stop_name}')"},
            ),
            self._get(
                f"{_BASE}/StopOfRoute/InterCity",
                {"$filter": f"Stops/any(s: s/StopName/Zh_tw eq '{stop_name}')"},
            ),
            return_exceptions=True,
        )
        return _safe_list(results[0]), _safe_list(results[1])

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
