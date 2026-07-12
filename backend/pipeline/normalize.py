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


# ── Streaming normalization ────────────────────────────────────────────────────

# Sentence-final punctuation; emission boundary for streamed LLM output.
_SENTENCE_END_RE = re.compile(r"[。！？!?\n]")
# Pause-level punctuation: once the pending text is long enough, emit at the
# last of these instead of waiting for a sentence end. Long enumerations
# (station lists joined by 、) would otherwise hold back the whole sentence.
# Fullwidth only — halfwidth , : ; appear inside times (09:58) and numbers,
# and cutting there corrupts the TTS digit reading.
_SOFT_BOUNDARY_RE = re.compile(r"[，、；]")
_SOFT_EMIT_MIN_CHARS = 20
# Safety valve: emit even without a boundary once the buffer grows this large,
# so a long clause without punctuation can't stall TTS indefinitely.
_EMIT_MAX_CHARS = 200
_THINK_OPEN = "<think>"
_THINK_CLOSE = "</think>"


class StreamNormalizer:
    """Incremental `normalize_llm_output` over a token stream.

    Feeds LLM content deltas, emits speakable pieces at sentence boundaries
    with <think> blocks stripped and s2twp applied per piece. Concatenating
    all emitted pieces equals the batch normalization modulo interior
    whitespace around stripped think blocks. Deviation from the batch rule:
    an *unclosed* <think> at stream end is dropped rather than kept — leaked
    reasoning must never reach TTS.
    """

    def __init__(self) -> None:
        self._raw = ""  # unscanned tail; may end with a partial <think> tag
        self._clean = ""  # think-free text waiting for a sentence boundary
        self._in_think = False
        self._emitted = False

    def feed(self, delta: str) -> list[str]:
        """Consume one content delta, return zero or more speakable pieces."""
        self._raw += delta
        self._scan_think_blocks()
        return self._emit_ready(final=False)

    def flush(self) -> list[str]:
        """Emit whatever remains at end of stream (drops an unclosed think block)."""
        self._scan_think_blocks()
        if not self._in_think:
            self._clean += self._raw
        self._raw = ""
        return self._emit_ready(final=True)

    def _scan_think_blocks(self) -> None:
        """Move think-free text from _raw to _clean, tracking tag state.

        Holds back a trailing partial tag prefix (e.g. "<thi") so a tag split
        across deltas is never emitted as text.
        """
        while True:
            if self._in_think:
                end = self._raw.find(_THINK_CLOSE)
                if end < 0:
                    return  # keep buffering inside the think block
                self._raw = self._raw[end + len(_THINK_CLOSE) :]
                self._in_think = False
            else:
                start = self._raw.find(_THINK_OPEN)
                if start >= 0:
                    self._clean += self._raw[:start]
                    self._raw = self._raw[start + len(_THINK_OPEN) :]
                    self._in_think = True
                    continue
                hold = 0
                for size in range(len(_THINK_OPEN) - 1, 0, -1):
                    if self._raw.endswith(_THINK_OPEN[:size]):
                        hold = size
                        break
                cut = len(self._raw) - hold
                self._clean += self._raw[:cut]
                self._raw = self._raw[cut:]
                return

    def _emit_ready(self, *, final: bool) -> list[str]:
        if final:
            cut = len(self._clean)
        else:
            sentence_ends = list(_SENTENCE_END_RE.finditer(self._clean))
            soft_ends = list(_SOFT_BOUNDARY_RE.finditer(self._clean))
            if sentence_ends:
                cut = sentence_ends[-1].end()
            elif len(self._clean) >= _SOFT_EMIT_MIN_CHARS and soft_ends:
                cut = soft_ends[-1].end()
            elif len(self._clean) >= _EMIT_MAX_CHARS:
                cut = len(self._clean)
            else:
                return []

        piece, self._clean = self._clean[:cut], self._clean[cut:]
        piece = _S2TWP.convert(piece)
        if not self._emitted:
            piece = piece.lstrip()
        if final:
            piece = piece.rstrip()
        if not piece:
            return []
        self._emitted = True
        return [piece]
