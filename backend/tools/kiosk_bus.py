"""Kiosk 公車查詢 facade（LLM agent 直接呼叫的 str 工具）。

外部入口：
  get_arrivals_here(route)             <- Kiosk 主要工具，站名從 KIOSK_STOP 取
  get_stop_arrival_statuses_here()     <- 本站全部路線目前到站狀態
  get_routes_at_stop(stop_name)        <- 指定站牌停靠路線，含常用站名縮寫
  get_route_stops(route)               <- 只查 Kiosk 站牌有停靠的路線

字串渲染與分類規則住 `services.departures`，本檔只負責：
  * 解析 KIOSK_STOP / KIOSK_DIRECTION 等 kiosk 範圍
  * 套用常用站名縮寫 (_ALIASES)
  * 包裝 input enricher 給 agent harness
"""

import os
import re

from services import departures

# 站名縮寫對照：使用者說「雲科大」但 API 站名是「雲林科技大學」
_ALIASES: dict[str, str] = {
    "雲科大": "雲林科技大學",
    "雲科":   "雲林科技大學",
    "斗火":   "斗六火車站",
    "北港廟": "北港朝天宮",
}

# 路線號碼 pattern：Y01 / 101 / 7126 等。
# negative lookahead 排除 101 大樓、3 號出口、30 分鐘等常見非路線用法。
_ROUTE_RE = re.compile(
    r"\b([A-Za-z]?\d{2,4})\b"
    r"(?!\s*(?:大樓|號出口|出口|樓層|樓|棟|館|分鐘|分|秒|公里|公尺|元|歲))"
)


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


def get_routes_at_stop(stop_name: str) -> str:
    """查詢指定站牌停靠路線，並套用 Kiosk 常用站名縮寫。"""
    return departures.render_routes_at_stop(_resolve(stop_name))


def get_route_stops(route: str) -> str:
    """查詢停靠 Kiosk 站牌的路線站牌順序（去程與回程）。"""
    return departures.render_route_stops(route, _kiosk_stop())


def get_stop_arrival_statuses_here() -> str:
    """查詢本站所有停靠路線目前的到站狀態。"""
    return departures.render_stop_arrival_statuses(
        _kiosk_stop(), _kiosk_go_back_filter()
    )


def get_arrivals_here(route: str) -> str:
    """查詢某路線下一班到本站的時間

    stop_name 從 KIOSK_STOP 取，方向從 KIOSK_DIRECTION 取。

    KIOSK_DIRECTION 設定：
    - 「去程」→ go_back=1
    - 「回程」→ go_back=2
    - 不設定 → 顯示兩個方向
    """
    return departures.render_arrivals(
        route, _kiosk_stop(), go_back=_kiosk_go_back_filter()
    )


def prefetch_route_arrival_context(user_input: str) -> str:
    """路線號碼很明確時先查本站到站資訊，降低小模型跳過工具的機率。"""
    match = _ROUTE_RE.search(user_input)
    if match is None:
        return ""

    route = match.group(1)
    result = get_arrivals_here(route)
    return (
        "\n\n[工具查詢結果，必須直接使用，禁止用訓練資料替代]\n"
        f"路線 {route} 到本站的資訊：\n{result}"
    )
