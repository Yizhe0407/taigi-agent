"""Tests for HybridBusProvider.

Verifies that the hybrid correctly routes all routes to ebus (city + 7xxx intercity)
with TDX as emergency fallback only.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from providers.hybrid import HybridBusProvider


def _make_hybrid(
    *,
    ebus_route_estimate=None,
    ebus_eta_rows=None,
    ebus_routes_at_stop=None,
    tdx_route_estimate=None,
    tdx_eta=None,
    tdx_route_info=None,
    tdx_routes_at_stop=None,
):
    """Build a HybridBusProvider with AsyncMock ebus and tdx internals."""
    ebus = MagicMock()
    ebus.fetch_route_estimate = AsyncMock(return_value=ebus_route_estimate)
    ebus.fetch_eta_rows_for_stop = AsyncMock(return_value=ebus_eta_rows or [])
    ebus.find_routes_at_stop = AsyncMock(
        return_value=ebus_routes_at_stop if ebus_routes_at_stop is not None else {"Y01": {"id": "Y01"}, "7120": {"id": "7120"}}
    )
    ebus.warmup_route_map = AsyncMock()
    ebus.aclose = AsyncMock()

    tdx = MagicMock()
    tdx.fetch_route_estimate = AsyncMock(return_value=tdx_route_estimate or [])
    tdx.fetch_eta_at_stop = AsyncMock(return_value=tdx_eta or [])
    tdx.load_route_info = AsyncMock(return_value=tdx_route_info or {"Y01": {"id": "Y01"}, "7120": {"id": "7120"}})
    tdx.fetch_routes_at_stop = AsyncMock(return_value=tdx_routes_at_stop or [])
    tdx.load_route_terminals = AsyncMock(return_value={"go_dest": "", "back_dest": ""})
    tdx.aclose = AsyncMock()

    return HybridBusProvider(ebus=ebus, tdx=tdx), ebus, tdx


# ── fetch_route_estimate routing ───────────────────────────────────────────────


def test_fetch_route_estimate_uses_ebus_when_available():
    ebus_rows = [{"stop_name": "A", "direction": 0, "stop_status": 0, "estimate_seconds": 120}]
    h, ebus, tdx = _make_hybrid(ebus_route_estimate=ebus_rows)

    result = asyncio.run(h.fetch_route_estimate("Y01"))

    assert result == ebus_rows
    ebus.fetch_route_estimate.assert_awaited_once_with("Y01")
    tdx.fetch_route_estimate.assert_not_awaited()


def test_fetch_route_estimate_falls_back_to_tdx_when_ebus_returns_none():
    """City route not in ebus (returns None) → falls back to TDX."""
    tdx_rows = [{"stop_name": "B", "direction": 1, "stop_status": 0, "estimate_seconds": 60}]
    h, ebus, tdx = _make_hybrid(ebus_route_estimate=None, tdx_route_estimate=tdx_rows)

    result = asyncio.run(h.fetch_route_estimate("Y99"))

    assert result == tdx_rows
    ebus.fetch_route_estimate.assert_awaited_once_with("Y99")
    tdx.fetch_route_estimate.assert_awaited_once_with("Y99")


def test_fetch_route_estimate_intercity_tries_ebus_first():
    """Intercity routes (7xxx) now go through ebus first — ebus has ComeTime so
    status 末班已過 (3) is correctly reported without relying on TDX."""
    ebus_rows = [{"stop_name": "C", "direction": 0, "stop_status": 3, "estimate_seconds": None}]
    h, ebus, tdx = _make_hybrid(ebus_route_estimate=ebus_rows)

    result = asyncio.run(h.fetch_route_estimate("7123A"))

    assert result == ebus_rows
    ebus.fetch_route_estimate.assert_awaited_once_with("7123A")
    tdx.fetch_route_estimate.assert_not_awaited()


# ── fetch_eta_at_stop routing ──────────────────────────────────────────────────


def test_fetch_eta_at_stop_passes_all_routes_to_ebus():
    """ebus is queried for ALL routes (city + 7xxx intercity) via find_routes_at_stop."""
    ebus_info = {"Y01": {"id": "Y01"}, "101": {"id": "101"}, "7120": {"id": "7120"}}
    ebus_rows = [
        {"sub_route_name": "Y01", "direction": 0, "stop_status": 0, "estimate_seconds": 60},
        {"sub_route_name": "7120", "direction": 0, "stop_status": 1, "estimate_seconds": None, "scheduled_time": "08:30"},
    ]

    h, ebus, tdx = _make_hybrid(ebus_eta_rows=ebus_rows, ebus_routes_at_stop=ebus_info)
    result = asyncio.run(h.fetch_eta_at_stop("斗六火車站"))

    all_names = set(ebus.fetch_eta_rows_for_stop.call_args.args[1])
    assert all_names == {"Y01", "101", "7120"}
    assert ebus_rows[0] in result
    assert ebus_rows[1] in result
    tdx.fetch_eta_at_stop.assert_not_awaited()


def test_fetch_eta_at_stop_tdx_fallback_when_ebus_returns_empty():
    """TDX full ETA is called only when ebus returns no rows at all."""
    ebus_info = {"7120": {"id": "7120"}}
    tdx_rows = [{"sub_route_name": "7120", "direction": 0, "stop_status": 0, "estimate_seconds": 300}]

    h, ebus, tdx = _make_hybrid(ebus_eta_rows=[], ebus_routes_at_stop=ebus_info, tdx_eta=tdx_rows)
    result = asyncio.run(h.fetch_eta_at_stop("斗六火車站"))

    assert result == tdx_rows
    tdx.fetch_eta_at_stop.assert_awaited_once_with("斗六火車站")


def test_fetch_eta_at_stop_ebus_failure_falls_back_to_tdx():
    """If ebus raises, TDX full ETA rows are returned."""
    tdx_rows = [{"sub_route_name": "7120", "direction": 0, "stop_status": 0, "estimate_seconds": 300}]

    h, ebus, tdx = _make_hybrid(tdx_eta=tdx_rows)
    ebus.fetch_eta_rows_for_stop.side_effect = RuntimeError("ebus down")

    result = asyncio.run(h.fetch_eta_at_stop("斗六火車站"))

    assert tdx_rows[0] in result


def test_fetch_eta_at_stop_tdx_not_called_when_ebus_has_data():
    """TDX ETA is NOT called when ebus returns rows."""
    ebus_info = {"Y01": {"id": "Y01"}, "101": {"id": "101"}}
    ebus_rows = [{"sub_route_name": "Y01", "direction": 0, "stop_status": 0, "estimate_seconds": 60}]

    h, ebus, tdx = _make_hybrid(ebus_eta_rows=ebus_rows, ebus_routes_at_stop=ebus_info)
    asyncio.run(h.fetch_eta_at_stop("斗六火車站"))

    tdx.fetch_eta_at_stop.assert_not_awaited()


def test_fetch_eta_at_stop_find_routes_failure_falls_back_to_tdx():
    """If ebus.find_routes_at_stop raises, TDX ETA is returned."""
    tdx_rows = [{"sub_route_name": "101", "direction": 0, "stop_status": 0, "estimate_seconds": 120}]

    h, ebus, tdx = _make_hybrid(tdx_eta=tdx_rows)
    ebus.find_routes_at_stop.side_effect = RuntimeError("ebus route scan failed")

    result = asyncio.run(h.fetch_eta_at_stop("斗六火車站"))

    assert result == tdx_rows


# ── Protocol delegation ────────────────────────────────────────────────────────


def test_load_route_info_delegates_to_ebus():
    info = {"101": {"id": "101", "go_dest": "斗六棒球場", "back_dest": "受天宮"}}
    h, ebus, tdx = _make_hybrid(ebus_routes_at_stop=info)
    result = asyncio.run(h.load_route_info("斗六火車站"))
    assert result == info
    ebus.find_routes_at_stop.assert_awaited_once_with("斗六火車站")
    tdx.load_route_info.assert_not_awaited()


def test_load_route_info_falls_back_to_tdx_when_ebus_raises():
    """TDX load_route_info is the emergency fallback when ebus fails."""
    tdx_info = {"101": {"id": "101", "go_dest": "斗六棒球場", "back_dest": "受天宮"}}
    h, ebus, tdx = _make_hybrid(tdx_route_info=tdx_info)
    ebus.find_routes_at_stop.side_effect = RuntimeError("ebus down")

    result = asyncio.run(h.load_route_info("斗六火車站"))

    assert result == tdx_info
    tdx.load_route_info.assert_awaited_once_with("斗六火車站")


def test_load_route_info_supplements_question_mark_dests_from_tdx():
    """Routes with '?' in ebus dest names are supplemented with correct names from TDX."""
    ebus_info = {"701": {"id": "701", "go_dest": "東??山莊", "back_dest": "斗六火車站"}}
    h, ebus, tdx = _make_hybrid(ebus_routes_at_stop=ebus_info)
    tdx.load_route_terminals = AsyncMock(return_value={"go_dest": "東𤧥山莊", "back_dest": ""})

    result = asyncio.run(h.load_route_info("雲林科技大學"))

    assert result["701"]["go_dest"] == "東𤧥山莊"
    assert result["701"]["back_dest"] == "斗六火車站"  # unchanged (no ?)
    tdx.load_route_terminals.assert_awaited_once_with("701")


def test_load_route_info_no_tdx_supplement_when_dests_clean():
    """TDX load_route_terminals is not called when ebus dest names have no '?'."""
    ebus_info = {"101": {"id": "101", "go_dest": "斗六棒球場", "back_dest": "受天宮"}}
    h, ebus, tdx = _make_hybrid(ebus_routes_at_stop=ebus_info)

    asyncio.run(h.load_route_info("斗六火車站"))

    tdx.load_route_terminals.assert_not_awaited()


def test_fetch_routes_at_stop_delegates_to_tdx():
    routes = [{"sub_route_name": "Y01", "direction": 0}]
    h, _, tdx = _make_hybrid(tdx_routes_at_stop=routes)
    result = asyncio.run(h.fetch_routes_at_stop("斗六火車站"))
    assert result == routes


def test_warmup_calls_ebus_route_map_and_route_discovery():
    h, ebus, _ = _make_hybrid()
    asyncio.run(h.warmup("斗六火車站"))
    ebus.warmup_route_map.assert_awaited_once()
    ebus.find_routes_at_stop.assert_awaited_once_with("斗六火車站")


def test_warmup_route_map_delegates_to_ebus():
    h, ebus, _ = _make_hybrid()
    asyncio.run(h.warmup_route_map())
    ebus.warmup_route_map.assert_awaited_once()


def test_aclose_closes_both():
    h, ebus, tdx = _make_hybrid()
    asyncio.run(h.aclose())
    ebus.aclose.assert_awaited_once()
    tdx.aclose.assert_awaited_once()


# ── provider.fallback metric ────────────────────────────────────────────────────


def test_fetch_route_estimate_records_ebus_hit():
    h, _, _ = _make_hybrid(ebus_route_estimate=[{"stop_name": "A"}])
    with patch("providers.hybrid.get_telemetry") as mock_get_telemetry:
        asyncio.run(h.fetch_route_estimate("Y01"))
    mock_get_telemetry.return_value.record_provider_fallback.assert_called_once_with(operation="route_estimate", outcome="ebus_hit")


def test_fetch_route_estimate_records_tdx_fallback():
    h, _, _ = _make_hybrid(ebus_route_estimate=None, tdx_route_estimate=[{"stop_name": "B"}])
    with patch("providers.hybrid.get_telemetry") as mock_get_telemetry:
        asyncio.run(h.fetch_route_estimate("Y99"))
    mock_get_telemetry.return_value.record_provider_fallback.assert_called_once_with(operation="route_estimate", outcome="tdx_fallback")


def test_fetch_eta_at_stop_records_ebus_hit():
    ebus_rows = [{"sub_route_name": "Y01", "direction": 0, "stop_status": 0, "estimate_seconds": 60}]
    h, _, _ = _make_hybrid(ebus_eta_rows=ebus_rows)
    with patch("providers.hybrid.get_telemetry") as mock_get_telemetry:
        asyncio.run(h.fetch_eta_at_stop("斗六火車站"))
    mock_get_telemetry.return_value.record_provider_fallback.assert_called_once_with(operation="eta", outcome="ebus_hit")


def test_fetch_eta_at_stop_records_tdx_fallback_when_tdx_has_rows():
    tdx_rows = [{"sub_route_name": "7120", "direction": 0, "stop_status": 0, "estimate_seconds": 300}]
    h, _, _ = _make_hybrid(ebus_eta_rows=[], tdx_eta=tdx_rows)
    with patch("providers.hybrid.get_telemetry") as mock_get_telemetry:
        asyncio.run(h.fetch_eta_at_stop("斗六火車站"))
    mock_get_telemetry.return_value.record_provider_fallback.assert_called_once_with(operation="eta", outcome="tdx_fallback")


def test_fetch_eta_at_stop_records_both_empty_when_tdx_also_empty():
    h, _, _ = _make_hybrid(ebus_eta_rows=[], tdx_eta=[])
    with patch("providers.hybrid.get_telemetry") as mock_get_telemetry:
        asyncio.run(h.fetch_eta_at_stop("斗六火車站"))
    mock_get_telemetry.return_value.record_provider_fallback.assert_called_once_with(operation="eta", outcome="both_empty")


def test_fetch_eta_at_stop_records_both_empty_when_tdx_raises():
    h, ebus, tdx = _make_hybrid()
    ebus.find_routes_at_stop.side_effect = RuntimeError("ebus down")
    tdx.fetch_eta_at_stop.side_effect = RuntimeError("tdx down")
    with patch("providers.hybrid.get_telemetry") as mock_get_telemetry:
        result = asyncio.run(h.fetch_eta_at_stop("斗六火車站"))
    assert result == []
    mock_get_telemetry.return_value.record_provider_fallback.assert_called_once_with(operation="eta", outcome="both_empty")
