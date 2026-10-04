"""Per-request time budget for upstream calls.

An interactive caller (an agent tool call answering a rider standing at the
kiosk) cannot wait out an upstream's ``Retry-After`` of 20-40 s, while a
background refresh can. Rather than threading a timeout argument through
every service and provider signature, the caller opens a budget with
``upstream_deadline(seconds)`` and upstream-facing code asks
``remaining_budget()`` before it would block:

- ``None`` → no budget in effect (background work); wait as long as needed.
- a float → seconds left; never block past it, raise ``UpstreamBudgetExceeded``
  instead so the caller can fall back to cached data or a "查不到" reply.

The budget lives in a ``ContextVar``, so it follows the request into tasks
spawned with ``asyncio.gather`` and never leaks into unrelated work.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_deadline: ContextVar[float | None] = ContextVar("upstream_deadline", default=None)


class UpstreamBudgetExceeded(Exception):
    """An upstream call would have blocked past the caller's time budget."""


@contextmanager
def upstream_deadline(seconds: float) -> Iterator[None]:
    """Bound upstream waits inside this block to `seconds` from now.

    Nested budgets never extend an outer one: the earlier deadline wins.
    """
    deadline = time.monotonic() + seconds
    outer = _deadline.get()
    token = _deadline.set(deadline if outer is None else min(outer, deadline))
    try:
        yield
    finally:
        _deadline.reset(token)


def remaining_budget() -> float | None:
    """Seconds left in the current budget (≥ 0), or None when none is in effect."""
    deadline = _deadline.get()
    if deadline is None:
        return None
    return max(0.0, deadline - time.monotonic())
