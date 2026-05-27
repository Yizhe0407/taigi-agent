"""雲林縣政府公車動態系統（ebus.yunlin.gov.tw）provider。

Pure I/O：HTTP fetch、路線名稱 → id 快取、方向標籤。
分類與字串呈現邏輯由 `services.departures` 負責，這一層只回 raw dict / 簡單衍生資料。
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


def get_routes_at_stop(stop_name: str) -> str:
    """查詢某站牌有哪些路線停靠（ebus.yunlin.gov.tw）。

    純粹列出路線、不牽涉 ETA 分類，所以仍留在 provider 層；其餘字串
    渲染搬到 `services.departures`。
    """
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
