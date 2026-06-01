"""TTS pre-normalizer: convert LLM output to TTS-safe Chinese before Tailo pipeline.

Applied in order:
1. Strip all bracket types, keep inner content.
2. Convert HH:MM times to Chinese time expressions.
3. Convert route codes (digit+optional-letter patterns + 路) digit-by-digit.
4. Convert minute/duration counts (N分, N分鐘) to Chinese.
5. Convert ordinal/count classifiers (第N班, N站後, N號).
6. Convert remaining Arabic digits digit-by-digit.
7. Convert remaining ASCII letters via phonetic table.
"""

from __future__ import annotations

import re

_DIGIT: dict[str, str] = {
    "0": "○", "1": "一", "2": "二", "3": "三", "4": "四",
    "5": "五", "6": "六", "7": "七", "8": "八", "9": "九",
}

_LETTER: dict[str, str] = {
    "A": "阿", "B": "逼", "C": "西", "D": "低", "E": "伊", "F": "夫",
    "G": "機", "H": "嘿", "I": "愛", "J": "傑", "K": "科", "L": "爾",
    "M": "姆", "N": "恩", "O": "喔", "P": "批", "Q": "球", "R": "阿兒",
    "S": "斯", "T": "替", "U": "優", "V": "肥", "W": "搭不溜",
    "X": "克斯", "Y": "歪", "Z": "賊",
}

# Hours use 兩 for 2, not 二
_HOUR_ZH: dict[int, str] = {
    1: "一", 2: "兩", 3: "三", 4: "四", 5: "五", 6: "六",
    7: "七", 8: "八", 9: "九", 10: "十", 11: "十一", 12: "十二",
}

_ONES = ["", "一", "二", "三", "四", "五", "六", "七", "八", "九"]


def _count_to_chinese(n: int) -> str:
    """Integer 1–99 → natural Chinese count (十二, 三十五…)."""
    if n <= 0:
        return "零"
    if n < 10:
        return _ONES[n]
    if n < 20:
        return "十" + _ONES[n % 10]
    tens = _ONES[n // 10] + "十"
    ones = _ONES[n % 10]
    return tens + ones


def _digits_to_chinese(s: str) -> str:
    return "".join(_DIGIT.get(c, c) for c in s)


def _letters_to_chinese(s: str) -> str:
    return "".join(_LETTER.get(c.upper(), "") for c in s)


def _time_sub(m: re.Match[str]) -> str:
    h, mn = int(m.group(1)), int(m.group(2))
    if h == 12:
        period, dh = "中午", 12
    elif h > 12:
        period, dh = "下午", h - 12
    else:
        period, dh = "", h
    h_zh = _HOUR_ZH.get(dh, _count_to_chinese(dh))
    if mn == 0:
        return f"{period}{h_zh}點"
    if mn < 10:
        return f"{period}{h_zh}點零{_count_to_chinese(mn)}分"
    return f"{period}{h_zh}點{_count_to_chinese(mn)}分"


def _route_sub(m: re.Match[str]) -> str:
    prefix = m.group(1) or ""
    digits = m.group(2)
    suffix = m.group(3) or ""
    return _letters_to_chinese(prefix) + _digits_to_chinese(digits) + _letters_to_chinese(suffix) + "路"


def normalize_for_tts(text: str) -> str:
    """Return a TTS-safe version of *text* with Arabic digits and ASCII letters converted."""
    if not text:
        return text

    # 1. Strip all bracket types, keep inner content
    text = re.sub(r"[「」『』【】《》〈〉]", "", text)
    text = re.sub(r"[（(]\s*([^）)]{0,40})\s*[）)]", r"\1", text)

    # 2. HH:MM → Chinese time
    # Use lookahead/lookbehind instead of \b: Python \w includes CJK chars,
    # so \b fails between a digit and a Chinese character.
    text = re.sub(r"(?<!\d)(\d{1,2}):(\d{2})(?!\d)", _time_sub, text)

    # 3. Route codes: optional-letter + 1–4 digits + optional-letter + 路
    text = re.sub(r"([A-Za-z]?)(\d{1,4})([A-Za-z]?)路", _route_sub, text)

    # 4. Minute/duration: N分鐘後?, N分後?, 約N分
    text = re.sub(
        r"(\d+)(分鐘後|分後|分鐘|分)",
        lambda m: _count_to_chinese(int(m.group(1))) + m.group(2),
        text,
    )

    # 5. Ordinal/count classifiers
    text = re.sub(
        r"第(\d+)(班|輛|站|號)",
        lambda m: "第" + _count_to_chinese(int(m.group(1))) + m.group(2),
        text,
    )
    text = re.sub(
        r"(\d+)(站後|站|班)",
        lambda m: _count_to_chinese(int(m.group(1))) + m.group(2),
        text,
    )

    # 6. Remaining Arabic digits → digit-by-digit
    text = re.sub(r"\d", lambda m: _DIGIT[m.group()], text)

    # 7. Remaining ASCII letters → phonetic Chinese
    text = re.sub(r"[A-Za-z]", lambda m: _LETTER.get(m.group().upper(), ""), text)

    return text
