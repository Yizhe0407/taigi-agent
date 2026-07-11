"""Kiosk 公車查詢 facade（LLM agent 直接呼叫的 str 工具）。

外部入口：
  get_arrivals_here(route)             <- Kiosk 主要工具，站名從 KIOSK_STOP 取
  get_stop_arrival_statuses_here()     <- 本站全部路線目前到站狀態
  get_routes_at_stop(stop_name)        <- 指定站牌停靠路線
  get_route_stops(route)               <- 只查 Kiosk 站牌有停靠的路線

字串渲染與分類規則住 `services.departures`，本檔只負責：
  * 解析 KIOSK_STOP / KIOSK_DIRECTION 等 kiosk 範圍
  * 確定性展開常見站名縮寫（見 `_ALIASES` / `_expand_alias`），
    再交給 `services.departures.normalize` 的 fuzzy remap 兜底
"""

from services import departures
from services.kiosk_config import kiosk_go_back_filter, kiosk_stop_name

# 常見口語縮寫 → 標準站名/地名的確定性對照表。
# 只做完全比對，不做模糊比對；模糊比對交給
# services/departures/normalize.py 的 _stop_similarity 兜底。
_ALIASES: dict[str, str] = {
    "雲科大": "雲林科技大學",
    "斗火": "斗六火車站",
    "北港廟": "北港朝天宮",
}


def _expand_alias(name: str) -> str:
    """完全比對命中則展開為標準站名，否則原樣傳回。"""
    return _ALIASES.get(name, name)


async def check_stop_on_route(route: str, stop_name: str) -> str:
    """查詢某路線是否停靠指定站牌（Python 精確比對，結果直接念給使用者）。"""
    return await departures.render_stop_on_route(route, _expand_alias(stop_name), kiosk_stop_name())


async def get_routes_at_stop(stop_name: str) -> str:
    """查詢指定站牌停靠路線。"""
    return await departures.render_routes_at_stop(_expand_alias(stop_name))


async def get_routes_at_stop_here() -> str:
    """查詢本站停靠路線（Router 直接呼叫，無需傳站名）。"""
    return await departures.render_routes_at_stop(kiosk_stop_name())


async def get_route_stops(route: str) -> str:
    """查詢停靠 Kiosk 站牌的路線站牌順序（去程與回程）。"""
    return await departures.render_route_stops(route, kiosk_stop_name())


async def get_stop_arrival_statuses_here() -> str:
    """查詢本站所有停靠路線目前的到站狀態。"""
    return await departures.render_stop_arrival_statuses(kiosk_stop_name(), kiosk_go_back_filter())


async def get_arrivals_to_destination(destination: str) -> str:
    """查詢本站哪些路線能到達目的地，並列出各路線下一班到站時間，依到站時間排序。"""
    return await departures.render_arrivals_to_destination(_expand_alias(destination), kiosk_stop_name(), go_back=kiosk_go_back_filter())


async def get_arrivals_here(route: str) -> str:
    """查詢某路線下一班到本站的時間

    stop_name 從 KIOSK_STOP 取，方向從 KIOSK_DIRECTION 取。

    KIOSK_DIRECTION 設定：
    - 「去程」→ direction=0
    - 「回程」→ direction=1
    - 不設定 → 顯示兩個方向
    """
    return await departures.render_arrivals(route, kiosk_stop_name(), go_back=kiosk_go_back_filter())
