from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from difflib import get_close_matches
from zoneinfo import ZoneInfo

from pipeline.normalize import count_to_chinese, to_halfwidth
from telemetry import get_telemetry

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


def _route_candidates(route: str, route_names: Iterable[str], limit: int = 5) -> list[str]:
    """Return up to `limit` route names most similar to `route`, best first.

    ASR mis-hearing rescue path for route numbers: matching is done on
    `_normalize_route_key` (halfwidth/uppercase/no trailing 路) so e.g. "301"
    vs "301路" still line up. cutoff=0.5 was picked by checking against a
    real 32-route stop list — it keeps single-digit-off numeric neighbors
    (e.g. "301" -> 701/302/201/101, all plausible ASR confusions) while
    dropping unrelated route names (e.g. "ABCDE" -> no matches).
    """
    key_to_name: dict[str, str] = {}
    query_key = _normalize_route_key(route)
    for name in route_names:
        key_to_name.setdefault(_normalize_route_key(name), name)
    matched_keys = get_close_matches(query_key, key_to_name.keys(), n=limit, cutoff=0.5)
    return [key_to_name[k] for k in matched_keys]


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
    """Return (name, score) pairs sorted by similarity, score > 0.25 only.

    Threshold is low: ASR mis-hearing 1-2 characters in a 3-character stop
    name can drop the charset Jaccard score to as low as 0.2-0.33, so 0.35
    would exclude the very mis-hearings this rescue path exists to catch.
    """
    scored = [(name, _stop_similarity(destination, name)) for name in stop_names if name != destination]
    scored.sort(key=lambda x: -x[1])
    return [(name, score) for name, score in scored if score > 0.25]


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


def _dedup_stop_rows_by_direction(rows: list[dict]) -> list[dict]:
    """Collapse duplicate stop occurrences to one row per direction.

    Circular routes yield two `fetch_route_estimate` rows for the kiosk stop
    name within the same direction — the boarding point (min stop_sequence)
    and the loop-completion arrival (max stop_sequence). Keeping the min-seq
    row matches `TdxBusProvider._build_route_info`'s boarding-UID selection,
    so callers see one consistent ETA instead of two conflicting ones.
    """
    best: dict[int, dict] = {}
    for row in rows:
        direction = row.get("direction", 0)
        seq = row.get("stop_sequence") or 9999
        existing = best.get(direction)
        if existing is None or (existing.get("stop_sequence") or 9999) > seq:
            best[direction] = row
    return list(best.values())


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
            get_telemetry().record_departure_decision(decision="filtered_terminal_direction")
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
    # Require both termini non-empty: empty string is a substring of everything,
    # so "" would cause a false-positive circular match.
    is_circular = go_dest and back_dest and _name_matches(stop_name, go_dest) and _name_matches(stop_name, back_dest)
    if is_circular:
        return False
    terminus = go_dest if direction == 0 else back_dest
    return bool(terminus) and _name_matches(stop_name, terminus)
