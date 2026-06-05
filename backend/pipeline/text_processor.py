"""Text conversion pipeline: Mandarin → 漢羅 (HanloFlow) → Tailo (Taibun).

Both converters are expensive to initialise (loads data artifacts from disk),
so they're held as module-level lazy singletons — one instance per process.

Usage::

    from pipeline.text_processor import process

    result = process("307 公車幾點到？")
    result.hanlo   # 漢羅混合文字
    result.tailo   # Tailo 台羅（送 TTS）
"""

from __future__ import annotations

from dataclasses import dataclass

from taibun import Converter as TaibunConverter  # type: ignore[import-untyped]
from taigi_converter import TaigiConverter  # type: ignore[import-untyped]

_hanlo_converter: TaigiConverter | None = None
_taibun_converter: TaibunConverter | None = None


def _get_hanlo() -> TaigiConverter:
    global _hanlo_converter
    if _hanlo_converter is None:
        _hanlo_converter = TaigiConverter()
    return _hanlo_converter


def _get_taibun() -> TaibunConverter:
    global _taibun_converter
    if _taibun_converter is None:
        # system="Tailo" = 台羅拼音；format="number" = 數字調號（Piper TTS 慣用格式）
        _taibun_converter = TaibunConverter(system="Tailo", format="number")
    return _taibun_converter


@dataclass
class TextProcessResult:
    hanlo: str  # 漢羅混合文字（中間產物，可用於 debug / admin 追蹤）
    tailo: str  # 台羅拼音（送 TTS）


def process(zh_text: str) -> TextProcessResult:
    """Convert Mandarin Chinese to Tailo romanization via 漢羅.

    Returns empty strings on empty input. Propagates conversion errors to caller.
    """
    if not zh_text.strip():
        return TextProcessResult(hanlo="", tailo="")

    hanlo = str(_get_hanlo().convert(zh_text))
    tailo = str(_get_taibun().get(hanlo)) if hanlo else ""
    return TextProcessResult(hanlo=hanlo, tailo=tailo)
