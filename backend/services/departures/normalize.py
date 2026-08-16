"""Shared normalization primitives for the departures package.

Two responsibilities used to live here as well and now have their own modules:

- `fuzzy_match.py` — ASR mis-hearing rescue: stop-name / route-number similarity
  scoring and candidate ranking.
- `rows.py` — TDX row shaping: grouping, dedup, direction filtering and
  downstream-stop derivation over raw provider rows.

What stays here is what both of them (and the renderers / snapshot layers) build
on: value normalization and the package clock. Both modules import from this one;
neither imports the other, so the dependency graph stays acyclic.
"""

from __future__ import annotations

import re
from zoneinfo import ZoneInfo

from pipeline.normalize import count_to_chinese, to_halfwidth

TAIPEI_TZ = ZoneInfo("Asia/Taipei")

_PAREN_RE = re.compile(r"\s*[（(][^）)]{0,40}[）)]\s*")


def _strip_paren(name: str) -> str:
    """Remove parenthetical suffixes: 持法媽祖宮(頂溪) → 持法媽祖宮."""
    return _PAREN_RE.sub("", name).strip()


def _mins_zh(n: int) -> str:
    """Integer minutes → natural Chinese count; >= 100 stays Arabic."""
    return str(n) if n >= 100 else count_to_chinese(n)


def _normalize_route_key(s: str) -> str:
    """Halfwidth + strip trailing 路 + uppercase for loose route lookup."""
    return to_halfwidth(s).rstrip("路").upper()


def _name_matches(needle: str, hay: str) -> bool:
    """Substring match in either direction so '斗六' matches '斗六火車站'."""
    return needle in hay or hay in needle
