"""Kiosk 公車查詢 facade。

外部入口：
  get_arrivals_here(route)  <- Kiosk 主要工具，站名從 KIOSK_STOP 取
  get_stop_arrival_statuses_here() <- 本站全部路線目前到站狀態
  get_routes_at_stop(stop_name) <- 指定站牌停靠路線，含常用站名縮寫
  get_route_stops(route)    <- 只查 Kiosk 站牌有停靠的路線

資料來源固定為 ebus.yunlin.gov.tw。路線 id 由站牌停靠清單解析，
讓 route lookup 跟 Kiosk 所在站牌綁在一起，避免同名路線歧義。
"""

import os

from tools import yunlin_ebus

# 站名縮寫對照：使用者說「雲科大」但 API 站名是「雲林科技大學」
_ALIASES: dict[str, str] = {
    "雲科大": "雲林科技大學",
    "雲科":   "雲林科技大學",
    "斗火":   "斗六火車站",
    "北港廟": "北港朝天宮",
}


def _resolve(stop_name: str) -> str:
    return _ALIASES.get(stop_name, stop_name)


def _kiosk_stop() -> str:
    return _resolve(os.getenv("KIOSK_STOP", "雲林科技大學"))


def _kiosk_go_back_filter() -> int | None:
    kiosk_dir = os.getenv("KIOSK_DIRECTION", "").strip()
    if kiosk_dir == "去程":
        return 1
    if kiosk_dir == "回程":
        return 2
    return None


def get_next_arrivals(
    route: str, stop_name: str, go_back_filter: int | None = None
) -> str:
    """查詢某路線在某站的下一班到站時間

    go_back_filter: 1=去程, 2=回程, None=兩個方向都顯示
    """
    return yunlin_ebus.get_arrivals(route, _resolve(stop_name), go_back=go_back_filter)


def get_routes_at_stop(stop_name: str) -> str:
    """查詢指定站牌停靠路線，並套用 Kiosk 常用站名縮寫。"""
    return yunlin_ebus.get_routes_at_stop(_resolve(stop_name))


def get_route_stops(route: str) -> str:
    """查詢停靠 Kiosk 站牌的路線站牌順序（去程與回程）。"""
    return yunlin_ebus.get_route_stops(route, _kiosk_stop())


def get_stop_arrival_statuses_here() -> str:
    """查詢本站所有停靠路線目前的到站狀態。"""
    return yunlin_ebus.get_stop_arrival_statuses(
        _kiosk_stop(), _kiosk_go_back_filter()
    )


def get_arrivals_here(route: str) -> str:
    """查詢某路線下一班到本站的時間

    stop_name 從 KIOSK_STOP 取，方向從 KIOSK_DIRECTION 取。

    KIOSK_DIRECTION 設定：
    - 「去程」→ go_back_filter=1
    - 「回程」→ go_back_filter=2
    - 不設定 → 顯示兩個方向
    """
    stop = _kiosk_stop()
    return get_next_arrivals(route, stop, go_back_filter=_kiosk_go_back_filter())
