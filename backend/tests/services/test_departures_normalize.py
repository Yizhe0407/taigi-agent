"""Unit tests for the ASR mis-hearing rescue matching in services.departures.normalize.

Covers the two structural gaps found in the 2026-07-12 rescue-rate eval
(20 cases, 20% full success): zero-character-overlap homophones (fuzzy stop
matching needs a pinyin dimension) and numeric route candidates getting
squeezed out of top-5 by ties (route matching needs edit-distance ranking).
"""

from services.departures.normalize import _fuzzy_candidates, _route_candidates, _stop_similarity

# ── fuzzy stop-name matching: pinyin rescue for homophone ASR errors ──────────


def test_stop_similarity_zero_char_overlap_homophone():
    """刺同→莿桐: zero shared characters, but identical pinyin (ci-tong). Eval case D4."""
    assert _stop_similarity("刺同", "莿桐") == 1.0


def test_stop_similarity_tone_mismatch_homophone():
    """背港→北港: 背(bei4) vs 北(bei3) differ only by tone. Eval case D3."""
    assert _stop_similarity("背港", "北港") == 1.0


def test_fuzzy_candidates_recovers_zero_char_overlap_homophone():
    stop_names = {"莿桐", "西螺", "斗六", "虎尾"}
    candidates = _fuzzy_candidates("刺同", stop_names)
    assert candidates
    assert candidates[0][0] == "莿桐"


def test_fuzzy_candidates_recovers_tone_mismatch_homophone():
    stop_names = {"北港", "古坑", "土庫", "梅山"}
    candidates = _fuzzy_candidates("背港", stop_names)
    assert candidates
    assert candidates[0][0] == "北港"


def test_fuzzy_candidates_does_not_regress_existing_char_overlap_case():
    """雲林高鐵站→高鐵雲林站: identical character set, no pinyin needed. Pre-existing behavior."""
    stop_names = {"高鐵雲林站", "斗六火車站"}
    candidates = _fuzzy_candidates("雲林高鐵站", stop_names)
    assert candidates[0] == ("高鐵雲林站", 1.0)


def test_fuzzy_candidates_does_not_regress_substring_alias_case():
    """斗溜火車戰→斗六火車站: mostly-shared characters, the original Jaccard rescue path."""
    stop_names = {"斗六火車站", "斗南火車站", "高鐵雲林站"}
    candidates = _fuzzy_candidates("斗溜火車戰", stop_names)
    assert candidates
    assert candidates[0][0] == "斗六火車站"


# ── numeric route matching: edit-distance ranking so ties don't squeeze out the target ──


def test_route_candidates_numeric_target_survives_many_tied_neighbors():
    """7112 mis-heard from 7121: with 8+ same-length '71xx'/'77xx' routes tying under
    difflib's ratio, 7121 must still make the top-5 instead of being cut by dict-order luck.
    Mirrors eval case R5 (real stop served 7120/7121/7122/7126/7132/7133/7138/7700)."""
    route_names = ["201", "701", "101", "301", "302", "7120", "7121", "7122", "7126", "7132", "7133", "7138", "7700"]
    candidates = _route_candidates("7112", route_names)
    assert "7121" in candidates
    assert len(candidates) <= 5


def test_route_candidates_numeric_prefers_same_length_over_length_change():
    """Same-position digit substitution (7132, 1 edit) must outrank a length-changing
    match (701, requires an insertion) even though both are 'close' under naive ratio."""
    route_names = ["701", "7132"]
    candidates = _route_candidates("7112", route_names)
    assert candidates[0] == "7132"


def test_route_candidates_numeric_does_not_regress_existing_case():
    """301 mis-heard: 201/701 (1 digit off) recovered, 7126 (different length/shape) excluded."""
    route_names = ["201", "701", "7126"]
    candidates = _route_candidates("301", route_names)
    assert "201" in candidates
    assert "701" in candidates
    assert "7126" not in candidates


def test_route_candidates_non_numeric_query_unaffected():
    """Non-numeric queries still use the original difflib path, unchanged."""
    assert _route_candidates("ABCDE", ["201", "701"]) == []
