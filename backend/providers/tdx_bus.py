"""TDX (tdx.transportdata.tw) concrete `BusProvider` implementation.

Covers both YunlinCounty city buses and intercity (公路客運) buses.

Route classification:
  Intercity: sub_route_name matches ^7\\d{3} (7000D, 7120, 7123A, …)
  City:      everything else (101, 201, 701, Y01, …)

fetch_route_estimate only queries the relevant endpoint (not both) to halve
request volume and avoid 429 rate limits. fetch_eta_at_stop and _stop_of_route
still query both because the caller doesn't know which routes are at the stop.
All parallel gather calls use return_exceptions=True so a 429 or timeout on
one endpoint degrades gracefully instead of killing the whole call.

Internal row schema — all methods return flat dicts with these keys:

  fetch_eta_at_stop / iter in iter_scoped_stop_etas:
    sub_route_name  str    SubRouteName.Zh_tw
    direction       int    0=去程 1=回程  (TDX Direction)
    stop_status     int    0=正常 1=未發車 2=不停 3=末班過 4=今日停駛
    estimate_seconds int|None  秒；None when stop_status != 0

  fetch_route_estimate:
    stop_name       str
    stop_sequence   int
    direction       int    0 / 1
    stop_status     int
    estimate_seconds int|None

  load_route_info → {sub_route_name: {"id": sub_route_name, "go_dest": str, "back_dest": str}}
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Callable

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
    """HTTP-backed `BusProvider` for tdx.transportdata.tw.

    Authenticates via OAuth2 client_credentials; token is cached until near
    expiry. Route-info cache lives on the instance and expires after
    `route_info_ttl_seconds`. Route-estimate cache expires after
    `route_estimate_ttl_seconds` (default 30 s — matches TDX update cadence).
    """

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        route_info_ttl_seconds: float | None = _DEFAULT_ROUTE_INFO_TTL,
        route_estimate_ttl_seconds: float | None = _DEFAULT_ROUTE_ESTIMATE_TTL,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._route_info_ttl = route_info_ttl_seconds
        self._route_estimate_ttl = route_estimate_ttl_seconds
        self._clock = clock
        # token cache
        self._token: str = ""
        self._token_expires_at: float = 0.0
        # stop_name → (fetched_at, route_info_dict)
        self._route_info_by_stop: dict[str, tuple[float, dict[str, dict]]] = {}
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
        resp = await http.get(
            url,
            params={**params, "$format": "JSON"},
            headers={"Authorization": f"Bearer {token}"},
            timeout=15.0,
        )
        resp.raise_for_status()
        return resp.json()

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
        info = self._build_route_info(city + intercity)
        self._route_info_by_stop[stop_name] = (self._clock(), info)
        return info

    async def aclose(self) -> None:
        pass  # shared http client; lifecycle managed by api lifespan

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
    def _build_route_info(records: list[dict]) -> dict[str, dict]:
        """Build sub_route_name → {id, go_dest, back_dest} from StopOfRoute records.

        Terminal names are derived from the last stop (by StopSequence) of each
        direction. direction=0 terminal → go_dest; direction=1 terminal → back_dest.
        """
        terminals: dict[tuple[str, int], str] = {}
        for rec in records:
            name = _zh(rec.get("SubRouteName"))
            if not name:
                continue
            direction = rec.get("Direction", 0)
            stops = rec.get("Stops") or []
            if not stops:
                continue
            ordered = sorted(stops, key=lambda s: s.get("StopSequence", 0))
            terminal = _zh(ordered[-1].get("StopName"))
            if terminal:
                terminals[(name, direction)] = terminal

        all_names = {name for name, _ in terminals}
        return {
            name: {
                "id": name,
                "go_dest": terminals.get((name, 0), ""),
                "back_dest": terminals.get((name, 1), ""),
            }
            for name in all_names
        }

    @staticmethod
    def _dedup_by_min_sequence(rows: list[dict]) -> list[dict]:
        """Keep one row per (sub_route_name, direction) — prefer lowest StopSequence.

        Circular routes (e.g., Y02) have the kiosk as both the first and last stop.
        TDX returns one ETA row per stop occurrence, so we get two rows: seq=1
        (departure, boarding-relevant) and seq=N (arrival of the same bus after
        completing the loop). Keeping the minimum-sequence row ensures the kiosk
        shows the departure status rather than the distant arrival time.
        """
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
