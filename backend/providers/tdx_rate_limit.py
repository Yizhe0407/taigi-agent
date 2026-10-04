"""Client-side rate limiter for TDX API keys.

TDX limits calls per API key by subscription tier (basic: 5/min, bronze:
5/s, …), and every TDX consumer in this process — bus ETA warmup, agent bus
tools, bike stations — shares the one key. Without a shared limiter they
discover the limit by colliding into HTTP 429 and sitting out Retry-After.

One `TdxRateLimiter` per key is shared process-wide (`tdx_rate_limiter`).
It enforces a sliding window — at most N requests in any window — and splits
it by who is waiting:

- Interactive callers (an `upstream_deadline` budget is in effect: a rider is
  waiting) may use the whole window. If a slot would open only after their
  budget, they get `UpstreamBudgetExceeded` at once, so the cache layer can
  answer with stale data instead.
- Background callers (no budget) may use only part of it, keeping
  `_INTERACTIVE_RESERVE` of every window free for riders; they wait as long
  as it takes.

A 429 that slips through anyway (another process on the same key, or TDX
counting differently) pauses the whole key for its Retry-After via
`penalize`, so other callers do not burn requests on the same rejection.

The limit comes from ``TDX_RATE_LIMIT`` ("5/min", "5/s"), defaulting to the
basic tier.
"""

from __future__ import annotations

import asyncio
import math
import os
import re
import threading
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from upstream_deadline import UpstreamBudgetExceeded, remaining_budget

_DEFAULT_RATE_LIMIT = "5/min"  # TDX basic member; set TDX_RATE_LIMIT after subscribing
# Share of every window kept free for interactive callers.
_INTERACTIVE_RESERVE = 0.4
_UNITS = {"s": 1.0, "sec": 1.0, "second": 1.0, "min": 60.0, "minute": 60.0, "h": 3600.0, "hour": 3600.0}
_RATE_RE = re.compile(r"^\s*(\d+)\s*/\s*([a-z]+)\s*$")


@dataclass(frozen=True, slots=True)
class RateLimit:
    requests: int
    per_seconds: float

    @property
    def background_requests(self) -> int:
        """Requests per window available to background callers (always ≥ 1)."""
        return max(1, self.requests - math.ceil(self.requests * _INTERACTIVE_RESERVE))


def parse_rate_limit(text: str) -> RateLimit:
    """Parse "N/s", "N/min" or "N/hour" (as TDX's pricing page states them)."""
    match = _RATE_RE.match(text.lower())
    if match is None or match.group(2) not in _UNITS or int(match.group(1)) < 1:
        raise ValueError(f"invalid TDX rate limit {text!r}; expected e.g. '5/min' or '5/s'")
    return RateLimit(requests=int(match.group(1)), per_seconds=_UNITS[match.group(2)])


class TdxRateLimiter:
    """Sliding-window limiter for one TDX API key (see module docstring)."""

    def __init__(
        self,
        limit: RateLimit,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._limit = limit
        self._clock = clock
        self._sleep = sleep
        self._sent: deque[float] = deque()  # start times of requests in the current window
        self._paused_until = 0.0

    @property
    def limit(self) -> RateLimit:
        return self._limit

    async def acquire(self) -> None:
        """Wait for a request slot, honouring the caller's upstream budget."""
        while True:
            wait = self._try_take()
            if wait <= 0:
                return
            budget = remaining_budget()
            if budget is not None and wait > budget:
                raise UpstreamBudgetExceeded(f"TDX rate limit: next slot in {wait:.1f}s exceeds the caller's budget")
            await self._sleep(wait)

    def penalize(self, seconds: float) -> None:
        """Pause the key after a 429: no caller sends until `seconds` from now."""
        self._paused_until = max(self._paused_until, self._clock() + max(0.0, seconds))

    def _try_take(self) -> float:
        """Take a slot and return 0, or return the seconds until one may open.

        Synchronous on purpose: check-and-take with no await in between is
        atomic on the event loop.
        """
        now = self._clock()
        window_start = now - self._limit.per_seconds
        while self._sent and self._sent[0] <= window_start:
            self._sent.popleft()
        if now < self._paused_until:
            return self._paused_until - now
        cap = self._limit.requests if remaining_budget() is not None else self._limit.background_requests
        if len(self._sent) < cap:
            self._sent.append(now)
            return 0.0
        # The slot frees when enough of the oldest requests leave the window.
        return self._sent[len(self._sent) - cap] + self._limit.per_seconds - now


_limiters: dict[str, TdxRateLimiter] = {}
_limiters_guard = threading.Lock()


def tdx_rate_limiter(client_id: str) -> TdxRateLimiter:
    """The process-wide limiter for `client_id` (TDX limits are per API key)."""
    with _limiters_guard:
        limiter = _limiters.get(client_id)
        if limiter is None:
            limiter = TdxRateLimiter(parse_rate_limit(os.getenv("TDX_RATE_LIMIT") or _DEFAULT_RATE_LIMIT))
            _limiters[client_id] = limiter
        return limiter


def reset_tdx_rate_limiters() -> None:
    """Forget every limiter (tests; picks up a changed TDX_RATE_LIMIT)."""
    with _limiters_guard:
        _limiters.clear()
