"""公車查詢 Router

決定用哪個資料來源，對 agent/tools.py 透明。

外部入口：
  get_arrivals_here(route)  ← Kiosk 主要工具，stop_name 從 KIOSK_STOP 環境變數取
  get_schedule(route)       ← 完整時刻表（TDX InterCity）/ 接下來班次（ebus）
  get_route_stops(route)    ← 路線站牌列表

路由規則：
  本地縣府路線（201 / Y01 / 101…）→ ebus.yunlin.gov.tw（yunlin_ebus）
  公路客運（7126 / 7720…）         → TDX InterCity（tdx）

為什麼 ebus 優先？TDX InterCity 也有同名的跨縣市「201」，
直接查 TDX 會拿到錯的資料。ebus 的 local provider 過濾確保找到雲林本地那條。
"""

import os
import requests

from tools import tdx, yunlin_ebus

# 站名縮寫對照：使用者說「雲科大」但 API 站名是「雲林科技大學」
# 放在 router 層，對兩個 source 都適用
_ALIASES: dict[str, str] = {
    "雲科大": "雲林科技大學",
    "雲科":   "雲林科技大學",
    "斗火":   "斗六火車站",
    "北港廟": "北港朝天宮",
}

_STOP_STATUS = {
    0: None,
    1: "尚未發車",
    2: "交管不停靠",
    3: "末班車已過",
    4: "今日未營運",
}


def _resolve(stop_name: str) -> str:
    return _ALIASES.get(stop_name, stop_name)


def get_next_arrivals(route: str, stop_name: str, go_back_filter: int | None = None) -> str:
    """查詢某路線在某站的下一班到站時間

    go_back_filter: 1=去程, 2=回程（ebus）/ 0=去程, 1=回程（TDX）
                    None=兩個方向都顯示
    """
    keyword = _resolve(stop_name)

    # ebus 優先：本地路線
    if yunlin_ebus._get_route_id(route) is not None:
        return yunlin_ebus.get_arrivals(route, keyword, go_back=go_back_filter)

    # TDX：公路客運（Direction 0=去程, 1=回程，與 ebus GoBack 1/2 不同）
    tdx_direction = None
    if go_back_filter == 1:
        tdx_direction = 0
    elif go_back_filter == 2:
        tdx_direction = 1

    try:
        matches = tdx.fetch_arrivals(route, keyword, direction=tdx_direction)
    except requests.HTTPError as e:
        if e.response.status_code == 404:
            return f"路線 {route} 在雲林縣公車及公路客運系統均查無資料"
        return f"TDX 查詢失敗：{e}"
    except Exception as e:
        return f"TDX 查詢失敗：{e}"

    if not matches:
        return f"路線 {route} 上找不到包含「{keyword}」的站牌"

    results = []
    for item in matches:
        status_code = item.get("StopStatus", 0)
        # HeadSign = TDX 提供的終點站顯示名（跟真實站牌一致）
        # 沒有 HeadSign 時退回去程/回程
        head_sign = (item.get("HeadSign") or "").strip()
        if head_sign:
            label = f"往{head_sign}"
        else:
            label = "去程" if item.get("Direction") == 0 else "回程"

        status_msg = _STOP_STATUS.get(status_code)
        if status_msg:
            results.append(f"{label}：{status_msg}")
        else:
            eta = item.get("EstimateTime")
            if eta is None:
                results.append(f"{label}：資料更新中")
            elif eta == 0:
                results.append(f"{label}：即將到站")
            else:
                results.append(f"{label}：約 {eta // 60} 分鐘後到站")

    return "\n".join(results)


def get_schedule(route: str) -> str:
    """查詢路線今日時刻表"""

    # ebus 優先：本地路線（只有接下來班次，非完整一日時刻表）
    if yunlin_ebus._get_route_id(route) is not None:
        return yunlin_ebus.get_schedule(route)

    # TDX：完整一日時刻表
    try:
        data = tdx.fetch_schedule(route)
    except requests.HTTPError as e:
        if e.response.status_code == 404:
            return f"路線 {route} 查無時刻表資料"
        return f"TDX 查詢失敗：{e}"
    except Exception as e:
        return f"TDX 查詢失敗：{e}"

    if not data:
        return f"路線 {route} 無時刻表資料"

    results = []
    for direction_data in data:
        direction = "去程" if direction_data.get("Direction") == 0 else "回程"
        timetables = direction_data.get("Timetables", [])
        if not timetables:
            results.append(f"{direction}：無時刻資料")
            continue

        times = sorted(
            st["DepartureTime"][:5]
            for trip in timetables
            for st in [trip.get("StopTimes", [{}])[0]]
            if st.get("DepartureTime")
        )
        first_stop = (
            timetables[0].get("StopTimes", [{}])[0]
            .get("StopName", {})
            .get("Zh_tw", "起點站")
        )
        results.append(f"{direction}（從{first_stop}出發）：{', '.join(times)}")

    return "\n".join(results)


def get_route_stops(route: str) -> str:
    """查詢路線上的所有站牌名稱（去程＋回程）"""

    if yunlin_ebus._get_route_id(route) is not None:
        return yunlin_ebus.get_route_stops(route)

    try:
        data = tdx.fetch_stops(route)
    except requests.HTTPError as e:
        if e.response.status_code == 404:
            return f"找不到路線 {route} 的站牌資料"
        return f"TDX 查詢失敗：{e}"
    except Exception as e:
        return f"TDX 查詢失敗：{e}"

    if not data:
        return f"找不到路線 {route} 的站牌資料"

    results = []
    for direction_data in data:
        direction = "去程" if direction_data.get("Direction") == 0 else "回程"
        stops = [s["StopName"]["Zh_tw"] for s in direction_data.get("Stops", [])]
        results.append(f"{direction}：{'→'.join(stops)}")

    return "\n".join(results)


def get_arrivals_here(route: str) -> str:
    """查詢某路線下一班到本站的時間

    stop_name 從 KIOSK_STOP 取，方向從 KIOSK_DIRECTION 取。

    KIOSK_DIRECTION 設定：
    - 「去程」→ go_back_filter=1（ebus），direction=0（TDX）
    - 「回程」→ go_back_filter=2（ebus），direction=1（TDX）
    - 不設定 → 顯示兩個方向
    """
    stop = os.getenv("KIOSK_STOP", "雲林科技大學")
    kiosk_dir = os.getenv("KIOSK_DIRECTION", "").strip()

    go_back_filter = None
    if kiosk_dir == "去程":
        go_back_filter = 1
    elif kiosk_dir == "回程":
        go_back_filter = 2

    return get_next_arrivals(route, stop, go_back_filter=go_back_filter)
