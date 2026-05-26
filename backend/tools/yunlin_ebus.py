"""雲林縣政府公車動態系統（ebus.yunlin.gov.tw）。

本專題以站牌 Kiosk 查詢為主，路線 id 會從指定站牌的停靠路線清單解析。
"""

import requests

_BASE = "https://ebus.yunlin.gov.tw/api"

# route cache: 站名 → 路線名稱 → {id, go_dest, back_dest}
# go_dest  = 去程（GoBack=1）的終點站名，用來顯示「往 go_dest」
# back_dest = 回程（GoBack=2）的終點站名，用來顯示「往 back_dest」
_route_info_by_stop: dict[str, dict[str, dict]] = {}


def _fetch_routes_at_stop(stop_name: str) -> list[dict]:
    resp = requests.get(
        f"{_BASE}/stop/route",
        params={"stop_name": stop_name},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def _fetch_eta_at_stop(stop_name: str) -> list[dict]:
    resp = requests.get(
        f"{_BASE}/stop/eta",
        params={"stop_name": stop_name},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def _fetch_route_estimate(route_id: int) -> list[dict]:
    resp = requests.get(f"{_BASE}/route/{route_id}/estimate", timeout=10)
    resp.raise_for_status()
    return resp.json()


def _load_route_info(stop_name: str) -> dict[str, dict]:
    """拿指定站牌的停靠路線，建立 route name -> route id cache。

    同名路線在全站資料中可能有歧義；站牌停靠清單會先把候選範圍縮到
    使用者所在站牌。若同一站牌仍出現同名但不同 id，寧可不選該名稱，
    避免 route name 靜默覆蓋。

    同時存起終點，讓輸出顯示「往高鐵雲林站」而不是「回程」，
    跟真實站牌的標示方式一致，使用者更容易理解。
    """
    cached = _route_info_by_stop.get(stop_name)
    if cached is not None:
        return cached

    route_info: dict[str, dict] = {}
    ambiguous_names: set[str] = set()
    for r in _fetch_routes_at_stop(stop_name):
        name = r.get("name")
        if not name or name in ambiguous_names:
            continue

        try:
            route_id = int(r["xno"])
        except (KeyError, TypeError, ValueError):
            continue

        existing = route_info.get(name)
        if existing is not None:
            if existing["id"] != route_id:
                route_info.pop(name)
                ambiguous_names.add(name)
            continue

        # destination = 去程終點，departure = 回程終點（也是去程起點）
        route_info[name] = {
            "id": route_id,
            "go_dest": r.get("destination", ""),
            "back_dest": r.get("departure", ""),
        }

    _route_info_by_stop[stop_name] = route_info
    return route_info


def _get_route_id(route: str, stop_name: str) -> int | None:
    info = _load_route_info(stop_name).get(route)
    return info["id"] if info else None


def _direction_label(route: str, stop_name: str, go_back: int) -> str:
    """把 GoBack 數字轉成『往 X』的標籤"""
    info = _load_route_info(stop_name).get(route, {})
    if go_back == 1:
        dest = info.get("go_dest", "")
        return f"往{dest}" if dest else "去程"
    else:
        dest = info.get("back_dest", "")
        return f"往{dest}" if dest else "回程"


def _arrival_status(stop: dict) -> str:
    """把 ebus estimate 的 Value/ComeTime 轉成站牌顯示狀態。"""
    value = stop.get("Value")
    if value is None:
        come = stop.get("ComeTime", "")
        return f"預定 {come}" if come else "未發車"
    if value == -3:
        return "末班駛離"
    if value <= 3:
        return "即將到站"
    return f"約 {value} 分鐘後"


def _arrival_section(stop: dict) -> str:
    value = stop.get("Value")
    if value == -3:
        return "末班駛離"
    if value is None and not stop.get("ComeTime", ""):
        return "未發車"
    return "尚有到站資訊"


def get_arrivals(route: str, stop_name: str, go_back: int | None = None) -> str:
    """查詢路線在某站的即時到站時間（ebus.yunlin.gov.tw）

    go_back: 1=去程, 2=回程, None=兩個方向都顯示
    """
    try:
        route_id = _get_route_id(route, stop_name)
    except Exception as e:
        return f"雲林公車查詢失敗：{e}"

    if route_id is None:
        return f"在「{stop_name}」找不到停靠路線 {route}"

    try:
        data = _fetch_route_estimate(route_id)
    except Exception as e:
        return f"雲林公車查詢失敗：{e}"

    matches = [
        s for s in data
        if stop_name in s.get("StopName", "")
        and (go_back is None or s.get("GoBack") == go_back)
    ]
    if not matches:
        return f"路線 {route} 上找不到包含「{stop_name}」的站牌"

    results = []
    for stop in matches:
        stop_go_back = stop.get("GoBack", 1)
        label = _direction_label(route, stop_name, stop_go_back)
        results.append(f"{label}：{_arrival_status(stop)}")

    return "\n".join(results)


def get_stop_arrival_statuses(stop_name: str, go_back: int | None = None) -> str:
    """查詢某站牌全部路線目前的到站狀態。"""
    try:
        eta_data = _fetch_eta_at_stop(stop_name)
        route_info = _load_route_info(stop_name)
    except Exception as e:
        return f"雲林公車查詢失敗：{e}"

    route_by_id = {info["id"]: name for name, info in route_info.items()}
    sections: dict[str, list[str]] = {
        "尚有到站資訊": [],
        "未發車": [],
        "末班駛離": [],
    }
    seen: set[str] = set()

    for stop in eta_data:
        stop_go_back = stop.get("GoBack", 1)
        if go_back is not None and stop_go_back != go_back:
            continue

        try:
            route_id = int(stop["xno"])
        except (KeyError, TypeError, ValueError):
            continue

        route = route_by_id.get(route_id)
        if route is None:
            continue

        label = _direction_label(route, stop_name, stop_go_back)
        line = f"{route} {label}：{_arrival_status(stop)}"
        if line in seen:
            continue

        seen.add(line)
        sections[_arrival_section(stop)].append(line)

    if not seen:
        return f"「{stop_name}」目前無到站狀態資料"

    results = [f"「{stop_name}」目前到站狀態："]
    for title, lines in sections.items():
        if lines:
            results.append(f"{title}：")
            results.extend(lines)

    return "\n".join(results)


def get_route_stops(route: str, stop_name: str) -> str:
    """查詢路線沿途站牌（去程＋回程）

    estimate endpoint 雖然名為「到站時間」，但它回傳路線上每一站的資料，
    包含 StopName、SeqNo、GoBack——剛好能重組出完整站牌順序。
    ebus 沒有獨立的 StopOfRoute endpoint，所以借用 estimate 的副產品。
    """
    try:
        route_id = _get_route_id(route, stop_name)
    except Exception as e:
        return f"雲林公車查詢失敗：{e}"

    if route_id is None:
        return f"在「{stop_name}」找不到停靠路線 {route}"

    try:
        data = _fetch_route_estimate(route_id)
    except Exception as e:
        return f"雲林公車查詢失敗：{e}"

    # 按方向分組，依 SeqNo 排序後取 StopName
    by_direction: dict[int, list[tuple[int, str]]] = {}
    for stop in data:
        go_back = stop.get("GoBack", 1)
        seq = stop.get("SeqNo", 0)
        name = stop.get("StopName", "")
        by_direction.setdefault(go_back, []).append((seq, name))

    if not by_direction:
        return f"路線 {route} 無站牌資料"

    results = []
    for go_back, stops in sorted(by_direction.items()):
        label = _direction_label(route, stop_name, go_back)
        ordered = [name for _, name in sorted(stops)]
        results.append(f"{label}：{'→'.join(ordered)}")

    return "\n".join(results)


def get_routes_at_stop(stop_name: str) -> str:
    """查詢某站牌有哪些路線停靠（ebus.yunlin.gov.tw）"""
    try:
        data = _fetch_routes_at_stop(stop_name)
    except Exception as e:
        return f"站名查詢失敗：{e}"

    if not data:
        return f"找不到「{stop_name}」這個站牌"

    # 同一條路線去回程會各出現一次，用 set 去重
    seen: set[str] = set()
    lines: list[str] = []
    for r in data:
        name = r.get("name", "?")
        if name not in seen:
            seen.add(name)
            desc = r.get("ddesc", "")
            lines.append(f"{name}（{desc}）")

    return f"「{stop_name}」停靠路線：\n" + "\n".join(lines)
