from __future__ import annotations

import re
from zoneinfo import ZoneInfo

from pipeline.normalize import to_halfwidth

TAIPEI_TZ = ZoneInfo("Asia/Taipei")

_PAREN_RE = re.compile(r"\s*[（(][^）)]{0,40}[）)]\s*")
_ONES_ZH = ["", "一", "二", "三", "四", "五", "六", "七", "八", "九"]
_STOP_SUFFIX = frozenset("站路街號市區鄉鎮村里")


def _strip_paren(name: str) -> str:
    """Remove parenthetical suffixes from stop names: 持法媽祖宮(頂溪) → 持法媽祖宮."""
    return _PAREN_RE.sub("", name).strip()


def _mins_zh(n: int) -> str:
    """Integer minutes → natural Chinese count."""
    if n <= 0:
        return "零"
    if n < 10:
        return _ONES_ZH[n]
    if n < 20:
        return "十" + _ONES_ZH[n % 10]
    if n < 100:
        return _ONES_ZH[n // 10] + "十" + _ONES_ZH[n % 10]
    return str(n)


def _fmt_time_12h(hhmm: str) -> str:
    """'HH:MM' → '上午/下午X點Y分'（TTS 友善）。"""
    h, m = map(int, hhmm.split(":"))
    period = "上午" if h < 12 else "下午"
    h12 = h % 12 or 12
    return f"{period}{h12}點" if m == 0 else f"{period}{h12}點{m}分"


def _normalize_route_key(s: str) -> str:
    """Halfwidth + strip trailing 路 + uppercase for loose route lookup."""
    return to_halfwidth(s).rstrip("路").upper()


def _lookup_route(route_info: dict, route: str) -> dict | None:
    """Case-insensitive route lookup that ignores trailing 路 and fullwidth."""
    key = _normalize_route_key(route)
    for name, info in route_info.items():
        if _normalize_route_key(name) == key:
            return info
    return None


def _name_matches(needle: str, hay: str) -> bool:
    """Substring match in either direction so '斗六' matches '斗六火車站'."""
    return needle in hay or hay in needle


def _stop_similarity(a: str, b: str) -> float:
    """Character-set union + bigram Jaccard similarity for Chinese station names.

    Strips common geographic suffixes so '雲林高鐵站' vs '高鐵雲林站' scores 1.0
    (identical character sets) and auto-remaps without LLM intervention.
    """
    a_core = [c for c in a if c not in _STOP_SUFFIX]
    b_core = [c for c in b if c not in _STOP_SUFFIX]
    if not a_core or not b_core:
        return 0.0
    a_set, b_set = set(a_core), set(b_core)
    char_ratio = len(a_set & b_set) / len(a_set | b_set)
    a_bi = {a_core[i] + a_core[i + 1] for i in range(len(a_core) - 1)}
    b_bi = {b_core[i] + b_core[i + 1] for i in range(len(b_core) - 1)}
    bi_ratio = len(a_bi & b_bi) / len(a_bi | b_bi) if (a_bi or b_bi) else 0.0
    return max(char_ratio, bi_ratio)


def _fuzzy_candidates(destination: str, stop_names: set[str]) -> list[tuple[str, float]]:
    """Return (name, score) pairs sorted by similarity, score > 0.35 only."""
    scored = [(name, _stop_similarity(destination, name)) for name in stop_names if name != destination]
    scored.sort(key=lambda x: -x[1])
    return [(name, score) for name, score in scored if score > 0.35]


def _as_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _stops_by_direction_with_seq(
    data: list[dict],
) -> dict[int, list[tuple[int, str]]]:
    """Group route_estimate rows by GoBack, retaining (seq, stripped_name)."""
    by_direction: dict[int, list[tuple[int, str]]] = {}
    for stop in data:
        gb = stop.get("GoBack", 1)
        seq = _as_int(stop.get("SeqNo"))
        if seq is None:
            continue
        name = _strip_paren(stop.get("StopName", ""))
        by_direction.setdefault(gb, []).append((seq, name))
    return by_direction


def _downstream_names(
    stops: list[tuple[int, str]],
    kiosk_stop: str,
) -> list[str] | None:
    """Return stop names at or after the first kiosk-matching position.

    Returns None when kiosk doesn't appear in this direction — caller should
    skip it (the bus on this direction doesn't pass through here at all).
    Includes the kiosk itself so「有沒有停 X 」when X is the kiosk answers 有.
    """
    kiosk_seq = next(
        (s for s, n in stops if _name_matches(kiosk_stop, n)),
        None,
    )
    if kiosk_seq is None:
        return None
    return [n for s, n in stops if s >= kiosk_seq]


def _direction_label_from_info(
    route_info: dict[str, dict],
    route: str,
    go_back: int,
) -> str:
    info = route_info.get(route, {})
    if go_back == 1:
        dest = info.get("go_dest", "")
        return f"往{dest}" if dest else "去程"
    dest = info.get("back_dest", "")
    return f"往{dest}" if dest else "回程"


def _is_terminal_direction(
    stop_name: str,
    route_info: dict[str, dict],
    route: str,
    go_back: int,
) -> bool:
    """True when this stop is the non-circular terminus for this direction.

    Circular routes (go_dest == back_dest == stop) always return False so
    they are shown — the bus departs from here even though it also returns here.
    """
    info = route_info.get(route, {})
    go_dest = info.get("go_dest", "")
    back_dest = info.get("back_dest", "")
    is_circular = _name_matches(stop_name, go_dest) and _name_matches(stop_name, back_dest)
    if is_circular:
        return False
    terminus = go_dest if go_back == 1 else back_dest
    return _name_matches(stop_name, terminus)
