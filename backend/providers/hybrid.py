"""HybridBusProvider: ebus primary, TDX as emergency fallback.

ebus.yunlin.gov.tw has no observed rate limits and covers both city routes and
7xxx intercity routes. ebus is used for all route discovery and ETA. TDX is
only called when ebus fails entirely.
"""

from __future__ import annotations

import asyncio
import logging

from providers.ebus import EbusBusProvider
from providers.tdx_bus import TdxBusProvider
from telemetry import get_telemetry

_log = logging.getLogger(__name__)


class HybridBusProvider:
    """BusProvider using ebus for all route discovery and ETA; TDX as emergency fallback.

    Protocol delegation:
      load_route_info       → ebus (find_routes_at_stop, cached 24h); TDX fallback
      fetch_routes_at_stop  → TDX  (agent tool, not on hot path)
      fetch_route_estimate  → ebus first (all routes); TDX fallback if not in ebus
      fetch_eta_at_stop     → ebus (all routes incl. 7xxx); TDX fallback if ebus down
    """

    def __init__(self, ebus: EbusBusProvider, tdx: TdxBusProvider) -> None:
        self._ebus = ebus
        self._tdx = tdx

    @property
    def ebus(self) -> EbusBusProvider:
        return self._ebus

    async def warmup(self, stop_name: str) -> None:
        """Pre-fetch route map and discover routes at stop_name at startup."""
        await self._ebus.warmup_route_map()
        await self._ebus.find_routes_at_stop(stop_name)

    async def warmup_route_map(self) -> None:
        """Pre-fetch ebus /api/route at startup (legacy; prefer warmup(stop_name))."""
        await self._ebus.warmup_route_map()

    # ── BusProvider protocol ───────────────────────────────────────────────────

    async def load_route_info(self, stop_name: str) -> dict[str, dict]:
        """ebus primary; TDX emergency fallback.

        ebus encodes CJK Extension B characters (>U+FFFF) as literal '?'.
        Routes with '?' in dest names are supplemented with TDX StopOfRoute data.
        """
        try:
            info = await self._ebus.find_routes_at_stop(stop_name)
        except Exception as exc:
            _log.warning("ebus find_routes_at_stop failed; falling back to TDX: %s", exc)
            return await self._tdx.load_route_info(stop_name)

        bad = [name for name, v in info.items() if "?" in v.get("go_dest", "") or "?" in v.get("back_dest", "")]
        for route_name in bad:
            try:
                terminals = await self._tdx.load_route_terminals(route_name)
                if terminals.get("go_dest"):
                    info[route_name]["go_dest"] = terminals["go_dest"]
                if terminals.get("back_dest"):
                    info[route_name]["back_dest"] = terminals["back_dest"]
            except Exception as exc:
                _log.warning("TDX terminal supplement failed for %s: %s", route_name, exc)

        return info

    async def fetch_routes_at_stop(self, stop_name: str) -> list[dict]:
        return await self._tdx.fetch_routes_at_stop(stop_name)

    async def fetch_route_estimate(self, sub_route_name: str) -> list[dict]:
        """ebus for all routes; TDX fallback if route not in ebus."""
        result = await self._ebus.fetch_route_estimate(sub_route_name)
        if result is not None:
            get_telemetry().record_provider_fallback(operation="route_estimate", outcome="ebus_hit")
            return result
        get_telemetry().record_provider_fallback(operation="route_estimate", outcome="tdx_fallback")
        return await self._tdx.fetch_route_estimate(sub_route_name)

    async def fetch_eta_at_stop(self, stop_name: str) -> list[dict]:
        """ebus for all routes (city + 7xxx intercity); TDX full fallback if ebus down."""
        try:
            route_info = await self._ebus.find_routes_at_stop(stop_name)
            rows = await self._ebus.fetch_eta_rows_for_stop(stop_name, list(route_info))
            if rows:
                get_telemetry().record_provider_fallback(operation="eta", outcome="ebus_hit")
                return rows
        except Exception as exc:
            _log.warning("ebus fetch_eta_at_stop failed for %s: %s", stop_name, exc)
        _log.warning("ebus returned nothing for %s; falling back to TDX", stop_name)
        try:
            tdx_rows = await self._tdx.fetch_eta_at_stop(stop_name)
            get_telemetry().record_provider_fallback(operation="eta", outcome="tdx_fallback" if tdx_rows else "both_empty")
            return tdx_rows
        except Exception as exc:
            _log.warning("TDX fallback also failed for %s: %s", stop_name, exc)
            get_telemetry().record_provider_fallback(operation="eta", outcome="both_empty")
            return []

    async def aclose(self) -> None:
        await asyncio.gather(
            self._ebus.aclose(),
            self._tdx.aclose(),
            return_exceptions=True,
        )
