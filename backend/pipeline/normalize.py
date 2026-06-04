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


def normalize_llm_output(text: str) -> str:
    """Strip <think> blocks and convert simplified Chinese to traditional."""
    return _S2TWP.convert(_THINK_RE.sub("", text).strip())
