"""TTS pre-normalizer: convert LLM output to TTS-safe Chinese before Tailo pipeline.

Applied in order:
1. Collapse newlines to commas.
2. Strip all bracket types, keep inner content.
3. Convert HH:MM times to Chinese time expressions (period-prefixed, then plain).
4. Convert route codes (digit+optional-letter patterns + 路) digit-by-digit.
5. Convert minute/duration counts (N分, N分鐘) to Chinese.
6. Convert ordinal/count classifiers (第N班, N站後, N號).
7. Convert remaining Arabic digits digit-by-digit.
8. Convert remaining ASCII letters via phonetic table.
"""

from __future__ import annotations

import re

from pipeline.normalize import (
    HOUR_ZH,
    count_to_chinese,
    digits_to_chinese,
)

_LETTER: dict[str, str] = {
    "A": "阿",
    "B": "逼",
    "C": "西",
    "D": "低",
    "E": "伊",
    "F": "夫",
    "G": "機",
    "H": "嘿",
    "I": "愛",
    "J": "傑",
    "K": "科",
    "L": "爾",
    "M": "姆",
    "N": "恩",
    "O": "喔",
    "P": "批",
    "Q": "球",
    "R": "阿兒",
    "S": "斯",
    "T": "替",
    "U": "優",
    "V": "肥",
    "W": "搭不溜",
    "X": "克斯",
    "Y": "歪",
    "Z": "賊",
}


def _letters_to_chinese(s: str) -> str:
    return "".join(_LETTER.get(c.upper(), "") for c in s)


def _format_time_zh(period: str, hour: int, minute: int) -> str:
    """`period` + hour:minute → '下午一點零五分' style Chinese time."""
    h_zh = HOUR_ZH.get(hour, count_to_chinese(hour))
    if minute == 0:
        return f"{period}{h_zh}點"
    if minute < 10:
        return f"{period}{h_zh}點零{count_to_chinese(minute)}分"
    return f"{period}{h_zh}點{count_to_chinese(minute)}分"


def _time_sub(m: re.Match[str]) -> str:
    h, mn = int(m.group(1)), int(m.group(2))
    if h == 0:
        period, dh = "凌晨", 0  # 00:xx is midnight — never bare "零點"
    elif h == 12:
        period, dh = "中午", 12
    elif h > 12:
        period, dh = "下午", h - 12
    else:
        period, dh = "", h
    return _format_time_zh(period, dh, mn)


def _route_sub(m: re.Match[str]) -> str:
    prefix = m.group(1) or ""
    digits = m.group(2)
    suffix = m.group(3) or ""
    return _letters_to_chinese(prefix) + digits_to_chinese(digits) + _letters_to_chinese(suffix) + "路"


def normalize_for_tts(text: str) -> str:
    """Return a TTS-safe version of *text* with Arabic digits and ASCII letters converted."""
    if not text:
        return text

    # 1. Collapse newlines — LLM may output multi-line; HanloFlow behavior unknown
    text = re.sub(r"\n+", "，", text)

    # 2. Strip all bracket types, keep inner content
    text = re.sub(r"[「」『』【】《》〈〉]", "", text)
    text = re.sub(r"[（(]\s*([^）)]{0,40})\s*[）)]", r"\1", text)

    # 3a. Period-prefixed 12h time: 下午/上午/中午/凌晨 + N:MM → Chinese
    # Must run before plain HH:MM step to avoid double-adding the period prefix.
    def _prefixed_time_sub(m: re.Match[str]) -> str:
        return _format_time_zh(m.group(1), int(m.group(2)), int(m.group(3)))

    text = re.sub(r"(下午|上午|中午|凌晨)(\d{1,2}):(\d{2})(?!\d)", _prefixed_time_sub, text)

    # 3b. Plain HH:MM → Chinese time (24h format, no prefix)
    # Use lookahead/lookbehind instead of \b: Python \w includes CJK chars,
    # so \b fails between a digit and a Chinese character.
    text = re.sub(r"(?<!\d)(\d{1,2}):(\d{2})(?!\d)", _time_sub, text)

    # 4. Route codes: optional-letter + 1–4 digits + optional-letter + 路
    text = re.sub(r"([A-Za-z]?)(\d{1,4})([A-Za-z]?)路", _route_sub, text)

    # 5. Minute/duration: N分鐘後?, N分後?, 約N分
    text = re.sub(
        r"(\d+)(分鐘後|分後|分鐘|分)",
        lambda m: count_to_chinese(int(m.group(1))) + m.group(2),
        text,
    )

    # 6. Ordinal/count classifiers
    text = re.sub(
        r"第(\d+)(班|輛|站|號)",
        lambda m: "第" + count_to_chinese(int(m.group(1))) + m.group(2),
        text,
    )
    text = re.sub(
        r"(\d+)(站後|站|班)",
        lambda m: count_to_chinese(int(m.group(1))) + m.group(2),
        text,
    )

    # 7. Remaining Arabic digits → digit-by-digit
    text = re.sub(r"\d+", lambda m: digits_to_chinese(m.group()), text)

    # 8. Remaining ASCII letters → phonetic Chinese
    text = re.sub(r"[A-Za-z]+", lambda m: _letters_to_chinese(m.group()), text)

    return text
