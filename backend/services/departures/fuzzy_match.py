"""ASR mis-hearing rescue: loose lookup and similarity ranking for names the
user spoke.

When a tool finds nothing for the stop or route the caller asked about, the
renderers hand back a candidate list from here and the LLM picks the
phonetically closest one before re-querying (see `agent/prompt.py`【聽錯救援】).
There is no hand-maintained abbreviation table — everything is scored.

Stop names and route numbers fail differently, so they are scored differently:
stop names by character/pinyin overlap (`_stop_similarity`, `_fuzzy_candidates`),
route numbers by a digit-confusion-weighted edit distance
(`_weighted_route_edit_distance`, `_route_candidates`).
"""

from __future__ import annotations

from collections.abc import Iterable
from difflib import SequenceMatcher, get_close_matches
from functools import lru_cache

from pypinyin import Style, pinyin

from services.departures.normalize import _normalize_route_key

_STOP_SUFFIX = frozenset("站路街號市區鄉鎮村里")


# Digit pairs that speech recognition routinely confuses, so a substitution
# between them is cheaper than an unrelated one and the true route outranks a
# mere arithmetic neighbour. (The previous abs()-based tie-break pulled every
# ambiguous "71xx" query toward the numerically central 7133.) Pairs, symmetric:
# 1/7 (Mandarin yī/qī, Taiwanese it/chhit), 0/4 (4 的尾音 vs 十/零), 2/8, 6/9
# (liù/jiǔ). Source: eval v5 route_digit_confusion failures R11/R13/R15.
_DIGIT_CONFUSION = frozenset(frozenset(pair) for pair in ("17", "04", "28", "69"))
_CONFUSED_SUB_COST = 0.4
_TRANSPOSE_COST = 0.9  # adjacent digit swap ("7112"→"7121"), a common ASR reorder (R5)


def _digit_sub_cost(a: str, b: str, base: float, *, leading: bool) -> float:
    if a == b:
        return 0.0
    # Never discount the leading digit: it selects the route *series*
    # (7xx vs 1xx), and discounting there made "702" resolve to 102 instead of
    # 701 (eval v6 iter-1 regression on R3). Observed ASR digit confusions are
    # all tail-position.
    if not leading and frozenset((a, b)) in _DIGIT_CONFUSION:
        return _CONFUSED_SUB_COST
    return base


def _weighted_route_edit_distance(a: str, b: str, *, sub_cost: float = 1.0, indel_cost: float = 2.0) -> float:
    """Damerau-Levenshtein distance tuned for route-number ASR errors.

    Route-number mis-hearings are same-length edits: a same-position digit swap
    ("7112"→"7132"), an adjacent transposition ("7112"→"7121"), or a
    phonetically-confused digit ("7134"→"7130", 0/4). Insertions/deletions cost
    2x substitution so same-length neighbours rank ahead of shorter/longer
    codes; confused-digit substitutions and transpositions cost <1 so the
    phonetic target beats an equidistant but unrelated route.
    """
    n, m = len(a), len(b)
    dp = [[0.0] * (m + 1) for _ in range(n + 1)]
    for i in range(n + 1):
        dp[i][0] = i * indel_cost
    for j in range(m + 1):
        dp[0][j] = j * indel_cost
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            dp[i][j] = min(
                dp[i - 1][j] + indel_cost,
                dp[i][j - 1] + indel_cost,
                dp[i - 1][j - 1] + _digit_sub_cost(a[i - 1], b[j - 1], sub_cost, leading=i == 1 and j == 1),
            )
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                dp[i][j] = min(dp[i][j], dp[i - 2][j - 2] + _TRANSPOSE_COST)
    return dp[n][m]


def _route_candidates(route: str, route_names: Iterable[str], limit: int = 5) -> list[str]:
    """Return up to `limit` route names most similar to `route`, best first.

    ASR mis-hearing rescue path for route numbers; matching runs on
    `_normalize_route_key` so "301" and "301路" line up.

    Digit-only queries rank by `_weighted_route_edit_distance` rather than
    `difflib`: a real stop can serve a dozen "71xx" codes, and
    `SequenceMatcher.ratio()` scores every single-digit-off neighbour
    identically, so which survive the top-`limit` cut becomes an accident of
    dict order — that used to squeeze the actual target out. Ties break on
    numeric closeness; the threshold (half the digit length, min 2) mirrors
    difflib's cutoff=0.5 of roughly half the digits differing.

    Everything else falls back to `difflib` at cutoff=0.5, which keeps
    single-digit-off and short-alias matches ("301" -> 701/302/201) while
    dropping unrelated names ("ABCDE" -> none).
    """
    key_to_name: dict[str, str] = {}
    query_key = _normalize_route_key(route)
    for name in route_names:
        key_to_name.setdefault(_normalize_route_key(name), name)

    if query_key.isdigit():
        numeric_keys = [k for k in key_to_name if k.isdigit()]
        if numeric_keys:
            threshold = max(2, len(query_key) // 2)
            scored = sorted(
                ((k, _weighted_route_edit_distance(query_key, k), abs(int(query_key) - int(k))) for k in numeric_keys),
                key=lambda x: (x[1], x[2]),
            )
            matched_keys = [k for k, dist, _ in scored if dist <= threshold][:limit]
            return [key_to_name[k] for k in matched_keys]

    matched_keys = get_close_matches(query_key, key_to_name.keys(), n=limit, cutoff=0.5)
    return [key_to_name[k] for k in matched_keys]


def _lookup_route(route_info: dict, route: str) -> dict | None:
    """Case-insensitive route lookup ignoring trailing 路 and fullwidth."""
    key = _normalize_route_key(route)
    for name, info in route_info.items():
        if _normalize_route_key(name) == key:
            return info
    return None


def _resolve_forward_match(query: str, names: Iterable[str]) -> str | None:
    """Canonical name for `query` among `names`, or None if not served here.

    Forward-only (`query` a substring of the real name — e.g. missing-suffix
    ASR errors like "西螺轉運" → "西螺轉運站"). Deliberately excludes the
    reverse direction (`normalize._name_matches`'s "real name is a substring of
    the query"): a short, unrelated real stop like "虎尾" is a substring of
    "虎尾科大" purely by prefix coincidence, and matching it directly
    pre-empts the fuzzy-rescue path that would otherwise correctly rank the
    actual target "虎尾科技大學" first. [eval E3] Ties (`query` is itself a
    valid stop name, so several downstream names contain it as a substring)
    go to the shortest match, i.e. the exact one.
    """
    forward = [n for n in names if query in n]
    return min(forward, key=len) if forward else None


def _token_jaccard(a_tokens: tuple[str, ...], b_tokens: tuple[str, ...]) -> float:
    """Set + adjacent-bigram Jaccard over arbitrary string tokens (chars or pinyin syllables)."""
    if not a_tokens or not b_tokens:
        return 0.0
    a_set, b_set = set(a_tokens), set(b_tokens)
    char_ratio = len(a_set & b_set) / len(a_set | b_set)
    a_bi = {a_tokens[i] + a_tokens[i + 1] for i in range(len(a_tokens) - 1)}
    b_bi = {b_tokens[i] + b_tokens[i + 1] for i in range(len(b_tokens) - 1)}
    bi_ratio = len(a_bi & b_bi) / len(a_bi | b_bi) if (a_bi or b_bi) else 0.0
    return max(char_ratio, bi_ratio)


@lru_cache(maxsize=512)
def _pinyin_syllables(core: tuple[str, ...]) -> tuple[str, ...]:
    """No-tone pinyin syllable per character, cached — the same station name is
    scored against many candidates, and pypinyin's per-call overhead adds up.
    """
    if not core:
        return ()
    return tuple(p[0] for p in pinyin(list(core), style=Style.NORMAL))


def _stop_similarity(a: str, b: str) -> float:
    """Character-token Jaccard, plus a no-tone-pinyin dimension for homophone ASR errors.

    Geographic suffixes are stripped so '雲林高鐵站' vs '高鐵雲林站' scores 1.0
    and auto-remaps without LLM intervention.

    The pinyin dimension exists because ASR mis-hearings are homophone-driven,
    not orthographic: "刺同" vs "莿桐" share zero characters yet sound
    identical. Tones are dropped — the substituted character usually keeps the
    syllable but not the tone ("背港" bei4-gang3 vs "北港" bei3-gang3).
    """
    a_core = [c for c in a if c not in _STOP_SUFFIX]
    b_core = [c for c in b if c not in _STOP_SUFFIX]
    if not a_core or not b_core:
        return 0.0
    char_score = _token_jaccard(tuple(a_core), tuple(b_core))
    pinyin_score = _token_jaccard(_pinyin_syllables(tuple(a_core)), _pinyin_syllables(tuple(b_core)))
    return max(char_score, pinyin_score)


def _core_pinyin_str(name: str) -> str:
    core = tuple(c for c in name if c not in _STOP_SUFFIX)
    return "".join(_pinyin_syllables(core))


def _ordered_pinyin_ratio(a: str, b: str) -> float:
    """Order-sensitive pinyin similarity (difflib ratio over syllable strings).

    Used only to break ties between candidates the set-based Jaccard scores
    identically: 林奈 vs 林內 and 大林 both share exactly one character with the
    query, but 林內 (linnei) keeps the query's syllable *order* (linnai) while
    大林 (dalin) reverses it — this ratio prefers 林內. Eval case D5.
    """
    return SequenceMatcher(None, _core_pinyin_str(a), _core_pinyin_str(b)).ratio()


def _fuzzy_candidates(destination: str, stop_names: set[str]) -> list[tuple[str, float]]:
    """Return (name, score) pairs sorted by similarity, score > 0.25 only.

    Threshold is low: ASR mis-hearing 1-2 characters in a 3-character stop
    name can drop the charset Jaccard score to as low as 0.2-0.33, so 0.35
    would exclude the very mis-hearings this rescue path exists to catch.
    Ties in the (order-insensitive) Jaccard score are broken by ordered pinyin
    similarity so a same-order homophone outranks a reordered one.
    """
    scored = [(name, _stop_similarity(destination, name), _ordered_pinyin_ratio(destination, name)) for name in stop_names if name != destination]
    scored.sort(key=lambda x: (-x[1], -x[2]))
    return [(name, score) for name, score, _ in scored if score > 0.25]
