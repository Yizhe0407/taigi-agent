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

import re

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

# 路線號碼 pattern：Y01 / 101 / 7126 / 7000b / 201A 等（前綴或後綴單字母）。
# negative lookahead 排除 101 大樓、3 號出口、30 分鐘等常見非路線用法。
_ROUTE_RE = re.compile(
    r"\b([A-Za-z]?\d{2,4}[A-Za-z]?)\b"
    r"(?!\s*(?:大樓|號出口|出口|樓層|樓|棟|館|分鐘|分|秒|公里|公尺|元|歲))"
)

# 純路線號碼輸入（不含問句或動詞）：「201」「7000b」「Y01路」等。
# 這類輸入應走 Rule 1 詢問使用者意圖，不預取以免小模型跳過 Rule 1 直接使用預取資料。
_ROUTE_ONLY_RE = re.compile(r"^[A-Za-z]?\d{1,4}[A-Za-z]?路?$")


def _resolve(stop_name: str) -> str:
    """Resolve user-input stop name aliases (for LLM tool calls, not kiosk config)."""
    return _ALIASES.get(stop_name, stop_name)


def _kiosk_stop() -> str:
    return get_kiosk_config().stop_name


def _kiosk_go_back_filter() -> int | None:
    direction = get_kiosk_config().direction
    if direction == "去程":
        return 1
    if direction == "回程":
        return 2
    return None


async def get_routes_at_stop(stop_name: str) -> str:
    """查詢指定站牌停靠路線，並套用 Kiosk 常用站名縮寫。"""
    return await departures.render_routes_at_stop(_resolve(stop_name))


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


async def prefetch_route_arrival_context(user_input: str) -> str:
    """路線號碼很明確時先查本站到站資訊，降低小模型跳過工具的機率。

    純路線號碼輸入（如「201」「7000b」）不預取：
    應走 Rule 1 詢問意圖，注入資料反而讓小模型繞過 Rule 1 直接回答。
    """
    if _ROUTE_ONLY_RE.match(user_input.strip()):
        return "\n\n[規則1：使用者只輸入路線號碼，請詢問想查什麼資訊，禁止呼叫任何工具]"
    match = _ROUTE_RE.search(user_input)
    if match is None:
        return ""

    route = match.group(1)
    result = await get_arrivals_here(route)
    return (
        "\n\n[預取到站資訊，僅供參考；仍須依決策規則判斷是否直接回應]\n"
        f"路線 {route} 到本站的資訊：\n{result}"
    )
