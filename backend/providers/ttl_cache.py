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
from collections.abc import AsyncIterator, Awaitable, Callable, MutableMapping
from contextlib import asynccontextmanager

_log = logging.getLogger(__name__)

# Sweep the store no more often than "once the live set has doubled", so the
# O(n) scan is O(1) amortised per store while keeping the key set within ~2x
# of what is actually unexpired.  Below this floor a sweep isn't worth running.
_MIN_SWEEP_THRESHOLD = 32


class _LockEntry:
    """An `asyncio.Lock` plus the number of tasks holding or queued for it."""

    __slots__ = ("lock", "users")

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.users = 0


class KeyedLocks[K]:
    """Per-key `asyncio.Lock` registry that forgets keys nobody is using.

    Coalescing (concurrent misses on one key collapsing into a single upstream
    fetch — losing this reintroduces the TDX 429 cascade) requires every
    contender to receive the *same* Lock object. A naive "delete on release"
    breaks that: a task arriving in the release→resume handoff window mints a
    second lock. `asyncio.Lock.locked()` can't detect that window either — it
    reads False while a released lock's next waiter hasn't resumed yet.

    Entries are therefore reference-counted: incremented before awaiting the
    lock, decremented after leaving the critical section, both in
    straight-line sync code (no await in between, so the event loop cannot
    interleave). A key's lock survives as long as at least one task is inside
    or queued for it, bounding the registry by in-flight concurrency rather
    than by the lifetime key space.
    """

    def __init__(self) -> None:
        self._entries: dict[K, _LockEntry] = {}

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, key: object) -> bool:
        return key in self._entries

    @asynccontextmanager
    async def acquire(self, key: K) -> AsyncIterator[None]:
        entry = self._entries.get(key)
        if entry is None:
            entry = _LockEntry()
            self._entries[key] = entry
        entry.users += 1
        try:
            async with entry.lock:
                yield
        finally:
            entry.users -= 1
            # Identity check: a previous holder may already have dropped this key
            # and a later arrival re-created it under a fresh entry.
            if entry.users == 0 and self._entries.get(key) is entry:
                del self._entries[key]


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
        self._locks: KeyedLocks[K] = KeyedLocks()
        self._sweep_threshold = _MIN_SWEEP_THRESHOLD

    def _expired(self, fetched_at: float, ttl: float | None) -> bool:
        if ttl is None or ttl <= 0:
            return False
        return (self._clock() - fetched_at) >= ttl

    def _maybe_sweep(self, retain_ttl: float | None) -> None:
        """Drop entries past `retain_ttl`, amortised so stores stay O(1).

        Read-time expiry alone would leave dead keys in the store forever, so
        this bounds it to "distinct keys seen within one retention window".

        `retain_ttl` is the caller's retention horizon, not the freshness TTL:
        callers using stale-serve pass `stale_ttl` here, not `ttl` — entries
        past `ttl` but within `stale_ttl` are still load-bearing and must
        survive the sweep.
        """
        if retain_ttl is None or retain_ttl <= 0:
            return  # no expiry configured — nothing is ever removable
        if len(self._store) <= self._sweep_threshold:
            return
        expired = [key for key, (fetched_at, _value) in self._store.items() if self._expired(fetched_at, retain_ttl)]
        for key in expired:
            del self._store[key]
        self._sweep_threshold = max(_MIN_SWEEP_THRESHOLD, 2 * len(self._store))

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

        async with self._locks.acquire(key):
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
            # Sweep after on_store so a caller-owned LRU trim has already run.
            self._maybe_sweep(stale_ttl if stale_ttl is not None else ttl)
            return value
