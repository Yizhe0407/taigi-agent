"""TDX 運輸資料流通服務 — 公路客運（InterCity）資料來源

只負責打 TDX API，不知道 ebus.yunlin.gov.tw 的存在。
適用路線：7126 / 7720 / 7700 / 7124 等公路客運（THB 開頭的 RouteUID）。
"""

import os
import time

import requests

_AUTH_URL = "https://tdx.transportdata.tw/auth/realms/TDXConnect/protocol/openid-connect/token"
_BASE_URL = "https://tdx.transportdata.tw/api/basic/v2/Bus"

_token_cache: dict = {"token": None, "expires_at": 0.0}


def _get_token() -> str:
    now = time.time()
    if _token_cache["token"] and now < _token_cache["expires_at"]:
        return _token_cache["token"]  # type: ignore[return-value]

    resp = requests.post(
        _AUTH_URL,
        data={
            "grant_type": "client_credentials",
            "client_id": os.getenv("TDX_CLIENT_ID"),
            "client_secret": os.getenv("TDX_CLIENT_SECRET"),
        },
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()

    _token_cache["token"] = data["access_token"]
    _token_cache["expires_at"] = now + data["expires_in"] - 60
    return data["access_token"]


def _get(path: str, params: dict | None = None) -> list:
    token = _get_token()
    resp = requests.get(
        f"{_BASE_URL}{path}",
        headers={"Authorization": f"Bearer {token}"},
        params={"$format": "JSON", **(params or {})},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def fetch_arrivals(route: str, keyword: str, direction: int | None = None) -> list:
    """TDX InterCity 即時到站資料，回傳原始 list 讓 router 格式化

    direction: 0=去程, 1=回程（TDX 的 Direction field），None=兩個方向
    """
    data = _get(f"/EstimatedTimeOfArrival/InterCity/{route}")
    return [
        item for item in data
        if keyword in item.get("StopName", {}).get("Zh_tw", "")
        and (direction is None or item.get("Direction") == direction)
    ]


def fetch_schedule(route: str) -> list:
    """TDX InterCity 時刻表，回傳原始 list 讓 router 格式化"""
    return _get(f"/Schedule/InterCity/{route}")


def fetch_stops(route: str) -> list:
    """TDX InterCity 站牌清單，回傳原始 list 讓 router 格式化"""
    return _get(f"/StopOfRoute/InterCity/{route}")
