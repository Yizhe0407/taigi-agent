"""Kiosk 公車查詢 facade（LLM agent 直接呼叫的 str 工具）。

外部入口：
  get_arrivals_here(route)             <- Kiosk 主要工具，站名從 KIOSK_STOP 取
  get_stop_arrival_statuses_here()     <- 本站全部路線目前到站狀態
  get_routes_at_stop(stop_name)        <- 指定站牌停靠路線，含常用站名縮寫
  get_route_stops(route)               <- 只查 Kiosk 站牌有停靠的路線

字串渲染與分類規則住 `services.departures`，本檔只負責：
  * 解析 KIOSK_STOP / KIOSK_DIRECTION 等 kiosk 範圍
  * 套用常用站名縮寫 (_ALIASES)
"""

from services import departures
from services.kiosk_config import get_kiosk_config

# 站名縮寫對照：LLM agent 工具接受使用者輸入的縮寫（e.g. 「雲科大」）
# Kiosk 設定的站牌名稱由 admin UI 選取，永遠是完整名稱，不需縮寫解析。
_ALIASES: dict[str, str] = {
    "雲科大": "雲林科技大學",
    "雲科":   "雲林科技大學",
    "斗火":   "斗六火車站",
    "北港廟": "北港朝天宮",
}


def _resolve(stop_name: str) -> str:
    """Resolve user-input stop name aliases (for LLM tool calls, not kiosk config)."""
    return _ALIASES.get(stop_name, stop_name)


def _kiosk_stop() -> str:
    return get_kiosk_config().stop_name


def _kiosk_go_back_filter() -> int | None:
    return get_kiosk_config().go_back


async def check_stop_on_route(route: str, stop_name: str) -> str:
    """查詢某路線是否停靠指定站牌（Python 精確比對，結果直接念給使用者）。"""
    return await departures.render_stop_on_route(route, _resolve(stop_name), _kiosk_stop())


async def find_routes_to_destination(destination: str) -> str:
    """查詢本站哪些路線能到達目的地（Python 平行查全部路線）。"""
    return await departures.render_routes_to_destination(_resolve(destination), _kiosk_stop())


async def get_routes_at_stop(stop_name: str) -> str:
    """查詢指定站牌停靠路線，並套用 Kiosk 常用站名縮寫。"""
    return await departures.render_routes_at_stop(_resolve(stop_name))


async def get_routes_at_stop_here() -> str:
    """查詢本站停靠路線（Router 直接呼叫，無需傳站名）。"""
    return await departures.render_routes_at_stop(_kiosk_stop())


async def get_route_stops(route: str) -> str:
    """查詢停靠 Kiosk 站牌的路線站牌順序（去程與回程）。"""
    return await departures.render_route_stops(route, _kiosk_stop())


async def get_stop_arrival_statuses_here() -> str:
    """查詢本站所有停靠路線目前的到站狀態。"""
    return await departures.render_stop_arrival_statuses(
        _kiosk_stop(), _kiosk_go_back_filter()
    )


async def get_arrivals_here(route: str) -> str:
    """查詢某路線下一班到本站的時間

    stop_name 從 KIOSK_STOP 取，方向從 KIOSK_DIRECTION 取。

    KIOSK_DIRECTION 設定：
    - 「去程」→ go_back=1
    - 「回程」→ go_back=2
    - 不設定 → 顯示兩個方向
    """
    return await departures.render_arrivals(
        route, _kiosk_stop(), go_back=_kiosk_go_back_filter()
    )


