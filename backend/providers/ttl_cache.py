"""Generic async TTL cache: hit/miss fast path + per-key lock + optional stale-serve.

Providers repeatedly hand-roll the same shape: check a `{key: (fetched_at,
value)}` dict, and on miss acquire a per-key `asyncio.Lock` (so concurrent
misses for the same key collapse into a single upstream fetch instead of a
"thundering herd"), re-check under the lock, then fetch and store.  Some
callers additionally want to keep serving the last-good value for a while
after the TTL expires if the upstream fetch fails ("stale-serve"), or to
run extra bookkeeping on a cache hit / store (e.g. LRU touch + eviction).

This class implements only that control flow. It does *not* own storage —
callers pass in their own dict/OrderedDict so existing code (and tests) that
reaches into `provider._some_cache` directly keeps working unchanged.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, MutableMapping

_log = logging.getLogger(__name__)


class TtlCache[K, V]:
    """Get-or-fetch control flow around a caller-owned `{key: (fetched_at, value)}` store."""

    def __init__(
        self,
        store: MutableMapping[K, tuple[float, V]],
        *,
        clock: Callable[[], float],
        cache_name: str = "",
        record_hit: Callable[[bool], None] | None = None,
    ) -> None:
        self._store = store
        self._clock = clock
        self._cache_name = cache_name
        self._record_hit = record_hit
        self._locks: dict[K, asyncio.Lock] = {}

    def _expired(self, fetched_at: float, ttl: float | None) -> bool:
        if ttl is None or ttl <= 0:
            return False
        return (self._clock() - fetched_at) >= ttl

    async def get_or_fetch(
        self,
        key: K,
        fetch: Callable[[], Awaitable[V]],
        *,
        ttl: float | None,
        stale_ttl: float | None = None,
        on_hit: Callable[[K], None] | None = None,
        on_store: Callable[[K, V], None] | None = None,
    ) -> V:
        """Return the cached value for `key`, or await `fetch()` and cache it.

        `fetch` only runs while holding this key's lock, so concurrent
        callers on a miss share one upstream call. If `fetch` raises and a
        value is cached within `stale_ttl` of `ttl` (i.e. not "too stale"),
        that value is served instead of propagating the error.
        """
        cached = self._store.get(key)
        hit = cached is not None and not self._expired(cached[0], ttl)
        if self._record_hit is not None:
            self._record_hit(hit)
        if cached is not None and hit:
            if on_hit is not None:
                on_hit(key)
            return cached[1]

        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            # Re-check after acquiring: first caller fills cache, subsequent callers hit it.
            cached = self._store.get(key)
            if cached is not None and not self._expired(cached[0], ttl):
                if on_hit is not None:
                    on_hit(key)
                return cached[1]

            try:
                value = await fetch()
            except Exception:
                if stale_ttl is not None and cached is not None and not self._expired(cached[0], stale_ttl):
                    _log.warning("%s fetch failed; serving stale cache for %r", self._cache_name, key)
                    return cached[1]
                raise

            self._store[key] = (self._clock(), value)
            if on_store is not None:
                on_store(key, value)
            return value
