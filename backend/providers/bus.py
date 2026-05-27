"""Real-time bus data provider Protocol.

Methods return raw payloads in the ebus.yunlin.gov.tw shape (`Value`,
`ComeTime`, `GoBack`, `SeqNo`, `StopName`, `xno`). Substitute implementations
must keep the same field semantics so `services.departures._classify_stop`
remains the single classification rule.
"""

from __future__ import annotations

from typing import Protocol


class BusProvider(Protocol):
    """Read-only view of a city's bus network.

    Implementations are expected to memoise `load_route_info` internally —
    services call it on every request.
    """

    async def fetch_routes_at_stop(self, stop_name: str) -> list[dict]:
        """Raw stop-route catalog row payload."""
        ...

    async def fetch_eta_at_stop(self, stop_name: str) -> list[dict]:
        """Raw ETA rows for every route currently serving `stop_name`."""
        ...

    async def fetch_route_estimate(self, route_id: int) -> list[dict]:
        """Raw ETA rows for every stop along a route (both directions)."""
        ...

    async def load_route_info(self, stop_name: str) -> dict[str, dict]:
        """`route_name -> {id, go_dest, back_dest}` for routes stopping here.

        Resolving by stop scopes ambiguous route names to the kiosk's
        candidate set (see `services.departures` callers).
        """
        ...
