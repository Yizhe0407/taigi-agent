"""ebus.yunlin.gov.tw concrete `BusProvider` implementation.

The upstream contract here is not owned by this project — payload shape is
treated as a moving target. Keep ebus-specific assumptions in this file so
the rest of the codebase can depend on the Protocol instead.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import httpx

from providers.bus import BusProvider

_DEFAULT_BASE = "https://ebus.yunlin.gov.tw/api"
_DEFAULT_TIMEOUT = 10.0
_DEFAULT_ROUTE_INFO_TTL_SECONDS = 600.0  # 10 min — ebus stop catalog rarely changes


class YunlinEbusProvider(BusProvider):
    """HTTP-backed `BusProvider` for ebus.yunlin.gov.tw.

    Route-info cache lives on the instance and expires after
    `route_info_ttl_seconds`. Pass `ttl=None` (or `0`) to disable expiry.

    The clock source is injected for tests — `monotonic` by default so the
    cache is immune to wall-clock jumps.
    """

    def __init__(
        self,
        base_url: str = _DEFAULT_BASE,
        timeout: float = _DEFAULT_TIMEOUT,
        *,
        route_info_ttl_seconds: float | None = _DEFAULT_ROUTE_INFO_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._base = base_url
        self._timeout = timeout
        self._ttl = route_info_ttl_seconds
        self._clock = clock
        # 站名 → (fetched_at, {路線名稱 → {id, go_dest, back_dest}})
        self._route_info_by_stop: dict[str, tuple[float, dict[str, dict]]] = {}

    # ── HTTP ──────────────────────────────────────────────────────────────────

    async def fetch_routes_at_stop(self, stop_name: str) -> list[dict]:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.get(
                f"{self._base}/stop/route",
                params={"stop_name": stop_name},
            )
        resp.raise_for_status()
        return resp.json()

    async def fetch_eta_at_stop(self, stop_name: str) -> list[dict]:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.get(
                f"{self._base}/stop/eta",
                params={"stop_name": stop_name},
            )
        resp.raise_for_status()
        return resp.json()

    async def fetch_route_estimate(self, route_id: int) -> list[dict]:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.get(f"{self._base}/route/{route_id}/estimate")
        resp.raise_for_status()
        return resp.json()

    # ── derived ───────────────────────────────────────────────────────────────

    def _build_route_info(self, rows: list[dict]) -> dict[str, dict]:
        route_info: dict[str, dict] = {}
        ambiguous_names: set[str] = set()
        for r in rows:
            name = r.get("name")
            if not name or name in ambiguous_names:
                continue

            try:
                route_id = int(r["xno"])
            except (KeyError, TypeError, ValueError):
                continue

            existing = route_info.get(name)
            if existing is not None:
                if existing["id"] != route_id:
                    route_info.pop(name)
                    ambiguous_names.add(name)
                continue

            route_info[name] = {
                "id": route_id,
                "go_dest": r.get("destination", ""),
                "back_dest": r.get("departure", ""),
            }
        return route_info

    async def load_route_info(self, stop_name: str) -> dict[str, dict]:
        """拿指定站牌的停靠路線，建立 route name -> {id, go_dest, back_dest} cache。

        同名路線在全站資料中可能有歧義；站牌停靠清單會先把候選範圍縮到
        使用者所在站牌。若同一站牌仍出現同名但不同 id，寧可不選該名稱，
        避免 route name 靜默覆蓋。

        同時存起終點，讓輸出顯示「往高鐵雲林站」而不是「回程」，
        跟真實站牌的標示方式一致。
        """
        cached = self._route_info_by_stop.get(stop_name)
        if cached is not None and not self._is_expired(cached[0]):
            return cached[1]

        route_info = self._build_route_info(await self.fetch_routes_at_stop(stop_name))
        self._route_info_by_stop[stop_name] = (self._clock(), route_info)
        return route_info

    def _is_expired(self, fetched_at: float) -> bool:
        if self._ttl is None or self._ttl <= 0:
            return False
        return (self._clock() - fetched_at) >= self._ttl
