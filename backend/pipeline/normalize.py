"""Text normalization utilities shared across agent, pipeline, and services layers."""

from __future__ import annotations

import re

import opencc

# Fullwidth ASCII (U+FF01–U+FF5E) → halfwidth (U+0021–U+007E)
_FULLWIDTH_RE = re.compile(r"[！-～]")

# Strip <think>…</think> blocks that reasoning models include in content
_THINK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL)

# Simplified → Traditional Chinese (Taiwan phonetic)
_S2TWP = opencc.OpenCC("s2twp")


def to_halfwidth(s: str) -> str:
    """Fullwidth ASCII → halfwidth for consistent regex matching."""
    return _FULLWIDTH_RE.sub(lambda m: chr(ord(m.group()) - 0xFEE0), s)


# ── Chinese numerals (shared by TTS pipeline + departure renderers) ────────────

DIGIT_ZH: dict[str, str] = {
    "0": "零",
    "1": "一",
    "2": "二",
    "3": "三",
    "4": "四",
    "5": "五",
    "6": "六",
    "7": "七",
    "8": "八",
    "9": "九",
}

# Hours use 兩 for 2 (spoken form), not 二.
# Hour 0 (24h midnight) speaks the same as 12 — "十二點" — distinguished only
# by the 凌晨/中午 period prefix, never "零點".
HOUR_ZH: dict[int, str] = {
    0: "十二",
    1: "一",
    2: "兩",
    3: "三",
    4: "四",
    5: "五",
    6: "六",
    7: "七",
    8: "八",
    9: "九",
    10: "十",
    11: "十一",
    12: "十二",
}

_ONES_ZH = ["", "一", "二", "三", "四", "五", "六", "七", "八", "九"]


def digits_to_chinese(s: str) -> str:
    """Digit-by-digit Chinese; non-digit characters pass through unchanged."""
    return "".join(DIGIT_ZH.get(c, c) for c in s)


def count_to_chinese(n: int) -> str:
    """Integer → natural Chinese count. n >= 100 falls back to digit-by-digit."""
    if n <= 0:
        return "零"
    if n < 10:
        return _ONES_ZH[n]
    if n < 20:
        return "十" + _ONES_ZH[n % 10]
    if n < 100:
        return _ONES_ZH[n // 10] + "十" + _ONES_ZH[n % 10]
    return digits_to_chinese(str(n))


def normalize_llm_output(text: str) -> str:
    """Strip <think> blocks and convert simplified Chinese to traditional."""
    return _S2TWP.convert(_THINK_RE.sub("", text).strip())
