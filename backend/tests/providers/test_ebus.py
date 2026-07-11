"""Tests for EbusBusProvider.

Covers route map caching, route-ID lookup (exact + suffix fallback),
estimate fetching / caching, stop-name filtering, and circular-route dedup.
"""

from __future__ import annotations

import asyncio

import httpx

import providers.ebus as ebus_module
from providers.ebus import (
    EbusBusProvider,
    _dedup_eta_by_min_seq,
    _norm_route_estimate_row,
    _terminals_from_estimate,
)

# ── Fake HTTP helpers ──────────────────────────────────────────────────────────


class _FakeResp:
    def __init__(self, payload, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                str(self.status_code),
                request=None,
                response=self,  # type: ignore[arg-type]
            )

    def json(self):
        return self._payload


def _patch_http(monkeypatch, responses: dict[str, object]):
    """Map URL substrings to response payloads.

    Matches longest key first so "/route/65036/estimate" takes priority
    over the shorter "/route" route-list key.
    """
    ordered = sorted(responses.items(), key=lambda kv: len(kv[0]), reverse=True)

    class FakeClient:
        async def get(self, url, **kwargs):
            for key, payload in ordered:
                if key in url:
                    data = payload() if callable(payload) else payload
                    return _FakeResp(data)
            raise AssertionError(f"Unexpected URL: {url}")

    monkeypatch.setattr(ebus_module, "get_http_client", lambda: FakeClient())


# ── Fixtures ───────────────────────────────────────────────────────────────────

_ROUTE_LIST = [
    {"Id": "65036", "NameZh": "201"},
    {"Id": "15121", "NameZh": "101"},
    {"Id": "12345", "NameZh": "Y01"},
    {"Id": "99001", "NameZh": "7120"},
]

_ESTIMATE_201 = [
    {"StopName": "高鐵雲林站", "GoBack": 1, "Value": 5, "SeqNo": 1},
    {"StopName": "斗六火車站", "GoBack": 1, "Value": 12, "SeqNo": 5},
    {"StopName": "雲林科技大學", "GoBack": 1, "Value": None, "SeqNo": 8},
    # return direction
    {"StopName": "雲林科技大學", "GoBack": 2, "Value": 2, "SeqNo": 1},
    {"StopName": "斗六火車站", "GoBack": 2, "Value": 3, "SeqNo": 3},
]

# Circular route: kiosk appears at seq=1 and seq=10 in the same direction
_ESTIMATE_CIRCULAR = [
    {"StopName": "斗六火車站", "GoBack": 1, "Value": 0, "SeqNo": 1},
    {"StopName": "中途站", "GoBack": 1, "Value": 5, "SeqNo": 5},
    {"StopName": "斗六火車站", "GoBack": 1, "Value": 25, "SeqNo": 10},
]


# ── Normalisation unit tests ───────────────────────────────────────────────────


def test_norm_route_estimate_row_running():
    row = {"StopName": "斗六火車站", "GoBack": 1, "Value": 5, "SeqNo": 3}
    out = _norm_route_estimate_row(row)
    assert out["stop_name"] == "斗六火車站"
    assert out["direction"] == 0  # GoBack 1 → direction 0
    assert out["stop_status"] == 0
    assert out["estimate_seconds"] == 300  # 5 min × 60
    assert out["stop_sequence"] == 3


def test_norm_route_estimate_row_zero_value():
    """Value=0 → 即將到站 (status 0, estimate_seconds=0)."""
    row = {"StopName": "本站", "GoBack": 1, "Value": 0, "SeqNo": 1}
    out = _norm_route_estimate_row(row)
    assert out["stop_status"] == 0
    assert out["estimate_seconds"] == 0


def test_norm_route_estimate_row_not_departed():
    """Value=None → 未發車 (status 1)."""
    row = {"StopName": "終點站", "GoBack": 2, "Value": None, "SeqNo": 8}
    out = _norm_route_estimate_row(row)
    assert out["direction"] == 1
    assert out["stop_status"] == 1
    assert out["estimate_seconds"] is None


def test_norm_route_estimate_row_last_departed():
    """Value=-3 (ebus sentinel) → 末班已過 (status 3)."""
    row = {"StopName": "終點站", "GoBack": 2, "Value": -3, "SeqNo": 8}
    out = _norm_route_estimate_row(row)
    assert out["stop_status"] == 3
    assert out["estimate_seconds"] is None


def test_dedup_eta_keeps_min_seq():
    rows = [
        {"sub_route_name": "Y02", "direction": 0, "stop_status": 0, "estimate_seconds": 0, "stop_sequence": 1},
        {"sub_route_name": "Y02", "direction": 0, "stop_status": 0, "estimate_seconds": 1500, "stop_sequence": 10},
    ]
    result = _dedup_eta_by_min_seq(rows)
    assert len(result) == 1
    assert result[0]["stop_sequence"] == 1  # boarding point, not loop-completion


# ── Route ID lookup ────────────────────────────────────────────────────────────


def test_get_route_id_exact_match(monkeypatch):
    _patch_http(monkeypatch, {"/route": _ROUTE_LIST})
    p = EbusBusProvider()
    assert asyncio.run(p.get_route_id("Y01")) == 12345


def test_get_route_id_alpha_suffix_fallback(monkeypatch):
    """7120A not in ebus; strips 'A' and finds 7120."""
    _patch_http(monkeypatch, {"/route": _ROUTE_LIST})
    p = EbusBusProvider()
    assert asyncio.run(p.get_route_id("7120A")) == 99001


def test_get_route_id_chinese_suffix_fallback(monkeypatch):
    _patch_http(monkeypatch, {"/route": _ROUTE_LIST})
    p = EbusBusProvider()
    # "101甲" → strip "甲" → "101"
    assert asyncio.run(p.get_route_id("101甲")) == 15121


def test_get_route_id_not_found(monkeypatch):
    _patch_http(monkeypatch, {"/route": _ROUTE_LIST})
    p = EbusBusProvider()
    assert asyncio.run(p.get_route_id("9999")) is None


# ── fetch_route_estimate ───────────────────────────────────────────────────────


def test_fetch_route_estimate_returns_none_when_not_in_ebus(monkeypatch):
    _patch_http(monkeypatch, {"/route": _ROUTE_LIST})
    p = EbusBusProvider()
    result = asyncio.run(p.fetch_route_estimate("999"))
    assert result is None


def test_fetch_route_estimate_normalises_rows(monkeypatch):
    _patch_http(monkeypatch, {"/route": _ROUTE_LIST, "65036/estimate": _ESTIMATE_201})
    p = EbusBusProvider()
    rows = asyncio.run(p.fetch_route_estimate("201"))
    assert rows is not None
    assert len(rows) == len(_ESTIMATE_201)
    # spot-check first row
    first = rows[0]
    assert first["stop_name"] == "高鐵雲林站"
    assert first["direction"] == 0
    assert first["estimate_seconds"] == 300


def test_fetch_route_estimate_caches(monkeypatch):
    call_count = 0

    def _estimate():
        nonlocal call_count
        call_count += 1
        return _ESTIMATE_201

    _patch_http(monkeypatch, {"/route": _ROUTE_LIST, "65036/estimate": _estimate})
    p = EbusBusProvider()
    asyncio.run(p.fetch_route_estimate("201"))
    asyncio.run(p.fetch_route_estimate("201"))
    # route list fetched once; estimate fetched once (second call hits cache)
    assert call_count == 1


# ── fetch_eta_rows_for_stop ────────────────────────────────────────────────────


def test_fetch_eta_rows_for_stop_filters_to_stop(monkeypatch):
    _patch_http(monkeypatch, {"/route": _ROUTE_LIST, "65036/estimate": _ESTIMATE_201})
    p = EbusBusProvider()
    rows = asyncio.run(p.fetch_eta_rows_for_stop("斗六火車站", ["201"]))
    stop_names = {r["sub_route_name"] for r in rows}
    assert stop_names == {"201"}
    # direction 0 (GoBack=1, seq=5) and direction 1 (GoBack=2, seq=3)
    assert len(rows) == 2


def test_fetch_eta_rows_for_stop_skips_unknown_route(monkeypatch):
    _patch_http(monkeypatch, {"/route": _ROUTE_LIST, "65036/estimate": _ESTIMATE_201})
    p = EbusBusProvider()
    rows = asyncio.run(p.fetch_eta_rows_for_stop("斗六火車站", ["201", "9999"]))
    # "9999" not in ebus → skipped; "201" matches
    assert all(r["sub_route_name"] == "201" for r in rows)


def test_fetch_eta_rows_for_stop_deduplicates_circular(monkeypatch):
    """Circular route stop appears twice; only boarding-point row kept (min seq)."""
    _patch_http(
        monkeypatch,
        {"/route": _ROUTE_LIST, "15121/estimate": _ESTIMATE_CIRCULAR},
    )
    p = EbusBusProvider()
    rows = asyncio.run(p.fetch_eta_rows_for_stop("斗六火車站", ["101"]))
    go_rows = [r for r in rows if r["direction"] == 0]
    assert len(go_rows) == 1
    assert go_rows[0]["stop_sequence"] == 1  # seq=1, not seq=10


def test_fetch_eta_rows_for_stop_eta_format(monkeypatch):
    """Returned rows must include sub_route_name, direction, stop_status, estimate_seconds."""
    _patch_http(monkeypatch, {"/route": _ROUTE_LIST, "65036/estimate": _ESTIMATE_201})
    p = EbusBusProvider()
    rows = asyncio.run(p.fetch_eta_rows_for_stop("斗六火車站", ["201"]))
    for row in rows:
        assert "sub_route_name" in row
        assert "direction" in row
        assert "stop_status" in row
        assert "estimate_seconds" in row


def test_fetch_eta_rows_for_stop_per_route_failure_does_not_abort(monkeypatch):
    """One route 404 → skipped; other routes still returned."""
    call_log: list[str] = []

    class FakeClient:
        async def get(self, url, **kwargs):
            if "/route" in url and "estimate" not in url:
                return _FakeResp(_ROUTE_LIST)
            if "65036/estimate" in url:
                call_log.append("201")
                return _FakeResp(_ESTIMATE_201)
            if "15121/estimate" in url:
                call_log.append("101-error")
                return _FakeResp({}, status_code=404)
            raise AssertionError(f"Unexpected URL: {url}")

    monkeypatch.setattr(ebus_module, "get_http_client", lambda: FakeClient())
    p = EbusBusProvider()
    rows = asyncio.run(p.fetch_eta_rows_for_stop("斗六火車站", ["201", "101"]))
    assert any(r["sub_route_name"] == "201" for r in rows)
    assert not any(r["sub_route_name"] == "101" for r in rows)


def test_fetch_eta_rows_shares_cache_with_fetch_route_estimate(monkeypatch):
    """fetch_route_estimate and fetch_eta_rows_for_stop use the same route cache."""
    call_count = 0

    def _estimate():
        nonlocal call_count
        call_count += 1
        return _ESTIMATE_201

    _patch_http(monkeypatch, {"/route": _ROUTE_LIST, "65036/estimate": _estimate})
    p = EbusBusProvider()
    asyncio.run(p.fetch_route_estimate("201"))  # warms cache
    asyncio.run(p.fetch_eta_rows_for_stop("斗六火車站", ["201"]))  # should hit cache
    assert call_count == 1


# ── _terminals_from_estimate ───────────────────────────────────────────────────


def test_terminals_from_estimate_basic():
    rows = [
        {"stop_name": "A", "stop_sequence": 1, "direction": 0},
        {"stop_name": "B", "stop_sequence": 5, "direction": 0},
        {"stop_name": "C", "stop_sequence": 10, "direction": 0},  # go_dest
        {"stop_name": "C", "stop_sequence": 1, "direction": 1},
        {"stop_name": "A", "stop_sequence": 10, "direction": 1},  # back_dest
    ]
    result = _terminals_from_estimate(rows)
    assert result["go_dest"] == "C"
    assert result["back_dest"] == "A"


def test_terminals_from_estimate_missing_direction():
    """Only direction=0 rows → back_dest empty."""
    rows = [
        {"stop_name": "A", "stop_sequence": 1, "direction": 0},
        {"stop_name": "Z", "stop_sequence": 20, "direction": 0},
    ]
    result = _terminals_from_estimate(rows)
    assert result["go_dest"] == "Z"
    assert result["back_dest"] == ""


def test_terminals_from_estimate_empty():
    assert _terminals_from_estimate([]) == {"go_dest": "", "back_dest": ""}


# ── find_routes_at_stop ────────────────────────────────────────────────────────

_ESTIMATE_7120 = [
    {"StopName": "斗六火車站", "GoBack": 1, "Value": 8, "SeqNo": 3},
    {"StopName": "虎尾", "GoBack": 1, "Value": 20, "SeqNo": 10},
    {"StopName": "虎尾", "GoBack": 2, "Value": 5, "SeqNo": 1},
    {"StopName": "斗六火車站", "GoBack": 2, "Value": 15, "SeqNo": 7},
]

_ESTIMATE_Y01 = [
    {"StopName": "斗六火車站", "GoBack": 1, "Value": 3, "SeqNo": 1},
    {"StopName": "雲科大", "GoBack": 1, "Value": 10, "SeqNo": 5},
]

_ESTIMATE_101 = [
    # 101 does not stop at 斗六火車站
    {"StopName": "西螺", "GoBack": 1, "Value": 5, "SeqNo": 1},
    {"StopName": "虎尾", "GoBack": 1, "Value": 15, "SeqNo": 5},
]

_ROUTE_LIST_EXTENDED = [
    {"Id": "65036", "NameZh": "201"},
    {"Id": "15121", "NameZh": "101"},
    {"Id": "12345", "NameZh": "Y01"},
    {"Id": "99001", "NameZh": "7120"},
]


def test_find_routes_at_stop_returns_matching_routes(monkeypatch):
    """Routes whose estimates include stop_name are returned; others excluded."""
    _patch_http(
        monkeypatch,
        {
            "/route": _ROUTE_LIST_EXTENDED,
            "65036/estimate": [],  # 201 — no stops match
            "15121/estimate": _ESTIMATE_101,  # 101 — does not stop here
            "12345/estimate": _ESTIMATE_Y01,  # Y01 — stops here
            "99001/estimate": _ESTIMATE_7120,  # 7120 — stops here
        },
    )
    p = EbusBusProvider()
    result = asyncio.run(p.find_routes_at_stop("斗六火車站"))

    assert "Y01" in result
    assert "7120" in result
    assert "101" not in result
    assert "201" not in result


def test_find_routes_at_stop_derives_terminals(monkeypatch):
    """go_dest/back_dest are derived from max-sequence stops."""
    _patch_http(
        monkeypatch,
        {
            "/route": [{"Id": "99001", "NameZh": "7120"}],
            "99001/estimate": _ESTIMATE_7120,
        },
    )
    p = EbusBusProvider()
    result = asyncio.run(p.find_routes_at_stop("斗六火車站"))

    assert result["7120"]["go_dest"] == "虎尾"  # max seq=10, direction=0
    assert result["7120"]["back_dest"] == "斗六火車站"  # max seq=7, direction=1


def test_find_routes_at_stop_cached(monkeypatch):
    """Second call hits cache; ebus route estimates are not re-fetched."""
    call_count = 0

    def _estimate_7120():
        nonlocal call_count
        call_count += 1
        return _ESTIMATE_7120

    _patch_http(
        monkeypatch,
        {
            "/route": [{"Id": "99001", "NameZh": "7120"}],
            "99001/estimate": _estimate_7120,
        },
    )
    p = EbusBusProvider()
    asyncio.run(p.find_routes_at_stop("斗六火車站"))
    asyncio.run(p.find_routes_at_stop("斗六火車站"))
    assert call_count == 1  # cache hit on second call


def test_find_routes_at_stop_serves_stale_on_empty_result(monkeypatch):
    """If scan returns empty, serve stale cache rather than empty dict."""
    p = EbusBusProvider()
    # Prime cache manually
    stale = {"7120": {"id": "7120", "go_dest": "虎尾", "back_dest": "斗六火車站"}}
    p._stop_route_cache["斗六火車站"] = (p._clock() - p._stop_route_ttl - 1, stale, False)

    # Return empty estimates for all routes
    _patch_http(
        monkeypatch,
        {
            "/route": [{"Id": "99001", "NameZh": "7120"}],
            "99001/estimate": [],
        },
    )
    result = asyncio.run(p.find_routes_at_stop("斗六火車站"))
    assert result == stale
