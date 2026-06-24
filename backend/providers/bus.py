"""Real-time bus data provider Protocol.

Methods return flat dicts in the TDX-normalised shape. See `providers/tdx_bus.py`
for the full field specification.

  fetch_eta_at_stop rows:    sub_route_name, direction (0/1), stop_status (0-4), estimate_seconds
  fetch_route_estimate rows: stop_name, stop_sequence, direction (0/1), stop_status, estimate_seconds
  fetch_routes_at_stop rows: sub_route_name, direction
  load_route_info values:    {id: str, go_dest: str, back_dest: str}

Direction encoding: 0 = 去程 (outbound), 1 = 回程 (inbound) — TDX native.
"""

from __future__ import annotations

from typing import Protocol


class BusProvider(Protocol):
    """Read-only view of a city's bus network.

    Implementations are expected to memoise `load_route_info` internally —
    services call it on every request.
    """

    async def fetch_routes_at_stop(self, stop_name: str) -> list[dict]:
        """Raw stop-route catalog rows (sub_route_name, direction)."""
        ...

    async def fetch_eta_at_stop(self, stop_name: str) -> list[dict]:
        """ETA rows for every subroute currently serving `stop_name`."""
        ...

    async def fetch_route_estimate(self, sub_route_name: str) -> list[dict]:
        """ETA rows for every stop along a subroute (both directions)."""
        ...

    async def load_route_info(self, stop_name: str) -> dict[str, dict]:
        """`sub_route_name -> {id, go_dest, back_dest}` for routes stopping here."""
        ...
