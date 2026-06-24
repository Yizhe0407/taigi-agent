from __future__ import annotations

import re
from collections.abc import Iterator
from zoneinfo import ZoneInfo

from pipeline.normalize import count_to_chinese, to_halfwidth

TAIPEI_TZ = ZoneInfo("Asia/Taipei")

_PAREN_RE = re.compile(r"\s*[（(][^）)]{0,40}[）)]\s*")
_STOP_SUFFIX = frozenset("站路街號市區鄉鎮村里")


def _strip_paren(name: str) -> str:
    """Remove parenthetical suffixes: 持法媽祖宮(頂溪) → 持法媽祖宮."""
    return _PAREN_RE.sub("", name).strip()


def _mins_zh(n: int) -> str:
    """Integer minutes → natural Chinese count; >= 100 stays Arabic."""
    return str(n) if n >= 100 else count_to_chinese(n)


def _normalize_route_key(s: str) -> str:
    """Halfwidth + strip trailing 路 + uppercase for loose route lookup."""
    return to_halfwidth(s).rstrip("路").upper()


def _lookup_route(route_info: dict, route: str) -> dict | None:
    """Case-insensitive route lookup ignoring trailing 路 and fullwidth."""
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
    """Group route_estimate rows by direction, retaining (seq, stripped_name).

    TDX fields: direction (0/1), stop_sequence (int), stop_name (str).
    """
    by_direction: dict[int, list[tuple[int, str]]] = {}
    for stop in data:
        direction = stop.get("direction", 0)
        seq = stop.get("stop_sequence")
        if seq is None:
            continue
        name = _strip_paren(stop.get("stop_name", ""))
        by_direction.setdefault(direction, []).append((seq, name))
    return by_direction


def _downstream_names(
    stops: list[tuple[int, str]],
    kiosk_stop: str,
) -> list[str] | None:
    """Return stop names at or after the first kiosk-matching position.

    Returns None when kiosk doesn't appear in this direction — caller should
    skip it.  Includes the kiosk itself so「有沒有停 X」when X is the kiosk
    answers 有.
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
    direction: int,
) -> str:
    """Return '往<dest>' label for the given TDX Direction (0=去程, 1=回程)."""
    info = route_info.get(route, {})
    if direction == 0:
        dest = info.get("go_dest", "")
        return f"往{dest}" if dest else "去程"
    dest = info.get("back_dest", "")
    return f"往{dest}" if dest else "回程"


def iter_scoped_stop_etas(
    eta_data: list[dict],
    route_info: dict[str, dict],
    stop_name: str,
    go_back: int | None,
) -> Iterator[tuple[dict, str, str, int]]:
    """Yield (stop_row, route, route_id, direction) for ETA rows in kiosk scope.

    TDX fields: sub_route_name, direction (0/1), stop_status.
    StopStatus 2 (交管不停靠) is silently skipped.
    go_back parameter uses TDX Direction encoding: 0=去程, 1=回程.
    """
    for stop in eta_data:
        if stop.get("stop_status") == 2:
            continue
        stop_direction = stop.get("direction", 0)
        sub_route_name = stop.get("sub_route_name")
        if not sub_route_name or sub_route_name not in route_info:
            continue

        if go_back is not None:
            if stop_direction != go_back:
                continue
        elif _is_terminal_direction(stop_name, route_info, sub_route_name, stop_direction):
            continue

        yield stop, sub_route_name, sub_route_name, stop_direction


def _is_terminal_direction(
    stop_name: str,
    route_info: dict[str, dict],
    route: str,
    direction: int,
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
    terminus = go_dest if direction == 0 else back_dest
    return _name_matches(stop_name, terminus)
