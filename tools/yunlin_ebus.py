"""雲林縣政府公車動態系統（ebus.yunlin.gov.tw）

TDX 沒有縣府自管路線（Y01、201 等）的資料，這個模組補上這個缺口。
不需要 API key，直接打縣政府後端。
"""

import requests

_BASE = "https://ebus.yunlin.gov.tw/api"

# 雲林縣本地公車業者 ProviderId
# 21: 台西客運等縣府路線, 22: 嘉義客運雲林段, 23: 員林客運縣府段, 34: 雲林縣市區客運, 124/135: 小型本地業者
# 跨縣市業者（國光/統聯等）ProviderId 為 27/32/45/56/61，不納入
_LOCAL_PROVIDER_IDS = {21, 22, 23, 34, 124, 135}

# route cache: 路線名稱 → {id, go_dest, back_dest}
# go_dest  = 去程（GoBack=1）的終點站名，用來顯示「往 go_dest」
# back_dest = 回程（GoBack=2）的終點站名，用來顯示「往 back_dest」
_route_info: dict[str, dict] | None = None


def _load_route_info() -> dict[str, dict]:
    """拿所有路線清單，建立路線查詢 dict

    為什麼只留本地業者？同名路線（如 201）在 ebus 裡可能對應到多條，
    ProviderId 過濾確保拿到雲林本地那條。

    為什麼同時存起終點？讓輸出顯示「往高鐵雲林站」而不是「回程」，
    跟真實站牌的標示方式一致，使用者更容易理解。
    """
    global _route_info
    if _route_info is not None:
        return _route_info

    resp = requests.get(f"{_BASE}/routes", timeout=10)
    resp.raise_for_status()

    _route_info = {}
    for r in resp.json():
        if r.get("ProviderId") not in _LOCAL_PROVIDER_IDS:
            continue
        name = r["NameZh"]
        # DepartureZh = 去程起點（GoBack=1 的出發站）
        # DestinationZh = 去程終點（GoBack=1 的終點，也是「往哪裡」的答案）
        _route_info[name] = {
            "id": r["Id"],
            "go_dest": r.get("DestinationZh", ""),    # 去程終點
            "back_dest": r.get("DepartureZh", ""),     # 回程終點（= 去程的起點）
        }

    return _route_info


def _get_route_id(route: str) -> int | None:
    info = _load_route_info().get(route)
    return info["id"] if info else None


def _direction_label(route: str, go_back: int) -> str:
    """把 GoBack 數字轉成『往 X』的標籤"""
    info = _load_route_info().get(route, {})
    if go_back == 1:
        dest = info.get("go_dest", "")
        return f"往{dest}" if dest else "去程"
    else:
        dest = info.get("back_dest", "")
        return f"往{dest}" if dest else "回程"


def get_arrivals(route: str, stop_name: str, go_back: int | None = None) -> str:
    """查詢路線在某站的即時到站時間（ebus.yunlin.gov.tw）

    go_back: 1=去程, 2=回程, None=兩個方向都顯示
    """
    route_id = _get_route_id(route)
    if route_id is None:
        return f"路線 {route} 在雲林縣公車系統找不到"

    try:
        resp = requests.get(f"{_BASE}/route/{route_id}/estimate", timeout=10)
        resp.raise_for_status()
        data = resp.json()
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
        go_back = stop.get("GoBack", 1)
        label = _direction_label(route, go_back)
        ests = stop.get("ESTs") or []

        if ests:
            mins = ests[0]["est"]
            suffix = "（末班）" if ests[0].get("isLast") else ""
            if mins == 0:
                results.append(f"{label}：即將到站{suffix}")
            else:
                results.append(f"{label}：約 {mins} 分鐘後{suffix}")
        elif stop.get("Value") is not None:
            results.append(f"{label}：約 {stop['Value']} 分鐘後")
        else:
            come = stop.get("ComeTime", "")
            if come:
                results.append(f"{label}：預定 {come}")
            else:
                results.append(f"{label}：無資料")

    return "\n".join(results)


def get_schedule(route: str) -> str:
    """查詢路線接下來的班次（ebus.yunlin.gov.tw）

    注意：ebus.yunlin.gov.tw 的 estimate API 只回傳當下視窗的接下來班次，
    不是完整的一整天時刻表。回傳的是目前到下一班的預定發車時間。
    """
    route_id = _get_route_id(route)
    if route_id is None:
        return f"路線 {route} 在雲林縣公車系統找不到"

    try:
        resp = requests.get(f"{_BASE}/route/{route_id}/estimate", timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        return f"雲林公車查詢失敗：{e}"

    # SeqNo == 1 是起點站，ComeTime 是這趟車從起點的發車時間
    by_direction: dict[int, list[str]] = {}
    for stop in data:
        if stop.get("SeqNo") == 1 and stop.get("ComeTime"):
            go_back = stop.get("GoBack", 1)
            by_direction.setdefault(go_back, []).append(stop["ComeTime"])

    if not by_direction:
        return f"路線 {route} 目前無接下來的班次資料"

    results = ["（注意：以下為接下來的班次，非完整今日時刻表）"]
    for go_back, times in sorted(by_direction.items()):
        direction = "去程" if go_back == 1 else "回程"
        first_stop = next(
            (s["StopName"] for s in data if s.get("GoBack") == go_back and s.get("SeqNo") == 1),
            "起點站",
        )
        results.append(f"{direction}（從{first_stop}出發）：{', '.join(sorted(times))}")

    return "\n".join(results)


def get_route_stops(route: str) -> str:
    """查詢路線沿途站牌（去程＋回程）

    estimate endpoint 雖然名為「到站時間」，但它回傳路線上每一站的資料，
    包含 StopName、SeqNo、GoBack——剛好能重組出完整站牌順序。
    ebus 沒有獨立的 StopOfRoute endpoint，所以借用 estimate 的副產品。
    """
    route_id = _get_route_id(route)
    if route_id is None:
        return f"路線 {route} 在雲林縣公車系統找不到"

    try:
        resp = requests.get(f"{_BASE}/route/{route_id}/estimate", timeout=10)
        resp.raise_for_status()
        data = resp.json()
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
        label = _direction_label(route, go_back)
        ordered = [name for _, name in sorted(stops)]
        results.append(f"{label}：{'→'.join(ordered)}")

    return "\n".join(results)


def get_routes_at_stop(stop_name: str) -> str:
    """查詢某站牌有哪些路線停靠（ebus.yunlin.gov.tw）"""
    try:
        resp = requests.get(
            f"{_BASE}/stop/route",
            params={"stop_name": stop_name},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
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
