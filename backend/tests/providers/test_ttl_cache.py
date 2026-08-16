"""Tests for TtlCache / KeyedLocks unbounded-growth fixes.

Three properties are load-bearing here:

  (a) neither the lock registry nor the store grows with the lifetime key space;
  (b) concurrent misses on one key still collapse into a single upstream fetch
      (losing this reintroduces the 429 cascade the per-key lock exists to stop);
  (c) expired entries are actually *removed*, not merely ignored on read.

No test uses sleeps or wall-clock timing: TTLs are driven by an injected clock,
and task interleaving is driven by explicit events plus `asyncio.sleep(0)`
scheduling yields (a yield to the event loop, not a timed wait).
"""

from __future__ import annotations

import asyncio

from providers.ttl_cache import KeyedLocks, TtlCache

# ── (b) coalescing ────────────────────────────────────────────────────────────


def test_concurrent_misses_on_same_key_trigger_one_fetch():
    """N concurrent misses on one key → exactly one upstream call."""

    async def scenario():
        store: dict[str, tuple[float, str]] = {}
        cache: TtlCache[str, str] = TtlCache(store, clock=lambda: 0.0)
        calls: list[int] = []
        gate = asyncio.Event()

        async def fetch() -> str:
            calls.append(1)
            await gate.wait()
            return "value"

        tasks = [asyncio.create_task(cache.get_or_fetch("k", fetch, ttl=10.0)) for _ in range(20)]
        # Deterministic: spin the event loop until all 20 have registered as
        # users of the key's lock (one scheduling step each), then release.
        while len(cache._locks) == 0 or cache._locks._entries["k"].users < 20:
            await asyncio.sleep(0)
        gate.set()
        results = await asyncio.gather(*tasks)
        return calls, results, cache

    calls, results, cache = asyncio.run(scenario())

    assert len(calls) == 1, "concurrent misses must coalesce into a single fetch"
    assert results == ["value"] * 20
    assert len(cache._locks) == 0, "lock entry must be dropped once nobody holds or awaits it"


def test_lock_entry_survives_while_a_waiter_is_queued():
    """The handoff-window regression: releasing must not orphan a queued waiter.

    A naive "del locks[key] on release" passes the test above (everyone already
    shares one Lock object) but fails here: it drops the registry entry the
    moment A leaves, so a task arriving while B is still queued mints a *second*
    lock and starts a duplicate fetch — coalescing silently degrades into a
    thundering herd.  This asserts the entry identity is preserved instead.
    """

    async def scenario():
        locks: KeyedLocks[str] = KeyedLocks()
        order: list[str] = []
        a_inside = asyncio.Event()
        a_release = asyncio.Event()
        b_release = asyncio.Event()

        async def worker(name: str, release: asyncio.Event, inside: asyncio.Event | None = None) -> None:
            async with locks.acquire("k"):
                order.append(f"enter:{name}")
                if inside is not None:
                    inside.set()
                await release.wait()
                order.append(f"exit:{name}")

        c_release = asyncio.Event()

        task_a = asyncio.create_task(worker("A", a_release, a_inside))
        await a_inside.wait()
        entry = locks._entries["k"]

        task_b = asyncio.create_task(worker("B", b_release))
        while entry.users < 2:  # B has queued on the same lock
            await asyncio.sleep(0)

        a_release.set()
        await task_a  # A has fully left its critical section and its finally block

        # B still holds/awaits the key, so the registry must still map "k" to the
        # *same* entry — otherwise a newcomer would build a competing lock.
        assert locks._entries.get("k") is entry, "entry dropped while a waiter was queued"

        # A genuine newcomer arriving after A released must join the same lock.
        task_c = asyncio.create_task(worker("C", c_release))
        while locks._entries["k"].users < 2:
            await asyncio.sleep(0)
        newcomer_shares_lock = locks._entries["k"] is entry

        b_release.set()
        c_release.set()
        await asyncio.gather(task_b, task_c)
        return order, locks, newcomer_shares_lock

    order, locks, newcomer_shares_lock = asyncio.run(scenario())

    assert newcomer_shares_lock, "a task arriving after a release must receive the same lock"
    assert order == ["enter:A", "exit:A", "enter:B", "exit:B", "enter:C", "exit:C"], "critical sections must not overlap"
    assert len(locks) == 0, "registry must be empty once every user has left"


def test_locks_do_not_accumulate_across_many_distinct_keys():
    """(a) The lock registry is bounded by in-flight concurrency, not key space."""

    async def scenario():
        store: dict[str, tuple[float, str]] = {}
        cache: TtlCache[str, str] = TtlCache(store, clock=lambda: 0.0)

        async def fetch() -> str:
            return "v"

        for index in range(500):
            await cache.get_or_fetch(f"k{index}", fetch, ttl=10.0)
        return cache

    cache = asyncio.run(scenario())
    assert len(cache._locks) == 0, "500 distinct keys must leave no residual locks"


# ── (a)/(c) store eviction ────────────────────────────────────────────────────


def test_store_is_bounded_and_expired_entries_are_removed():
    """(a)+(c) Expired keys are deleted, so the store does not grow monotonically."""

    async def scenario():
        now = [0.0]
        store: dict[str, tuple[float, str]] = {}
        cache: TtlCache[str, str] = TtlCache(store, clock=lambda: now[0])
        sizes: list[int] = []

        async def fetch() -> str:
            return "v"

        for index in range(500):
            await cache.get_or_fetch(f"k{index}", fetch, ttl=10.0)
            sizes.append(len(store))
            now[0] += 20.0  # each entry is expired well before the next store
        return store, sizes, cache

    store, sizes, cache = asyncio.run(scenario())

    assert len(store) < 64, f"store must stay bounded, got {len(store)} entries for 500 keys"
    assert max(sizes) < 64, f"store peaked at {max(sizes)} — growth is not bounded"
    # (c) removal, not read-time masking: old keys are gone from the mapping itself.
    assert "k0" not in store
    assert "k100" not in store
    assert len(cache._locks) == 0


def test_sweep_keeps_entries_still_within_stale_ttl():
    """Stale-serve data must survive the sweep: retention uses stale_ttl, not ttl."""

    async def scenario():
        now = [0.0]
        store: dict[str, tuple[float, str]] = {}
        cache: TtlCache[str, str] = TtlCache(store, clock=lambda: now[0])

        async def fetch() -> str:
            return "v"

        for index in range(40):
            await cache.get_or_fetch(f"old{index}", fetch, ttl=10.0, stale_ttl=1000.0)
        now[0] = 100.0  # past ttl, well inside stale_ttl
        # Enough further stores to actually cross the sweep threshold, so the
        # sweep genuinely runs against the aged entries rather than skipping.
        for index in range(30):
            await cache.get_or_fetch(f"new{index}", fetch, ttl=10.0, stale_ttl=1000.0)
        return store

    store = asyncio.run(scenario())
    assert "old0" in store, "entry inside stale_ttl must not be swept — stale-serve depends on it"
    assert sum(1 for key in store if key.startswith("old")) == 40
    assert len(store) == 70


def test_sweep_never_runs_without_a_ttl():
    """ttl=None means 'never expires'; the sweep must not invent an expiry."""

    async def scenario():
        store: dict[str, tuple[float, str]] = {}
        cache: TtlCache[str, str] = TtlCache(store, clock=lambda: 0.0)

        async def fetch() -> str:
            return "v"

        for index in range(100):
            await cache.get_or_fetch(f"k{index}", fetch, ttl=None)
        return store

    store = asyncio.run(scenario())
    assert len(store) == 100


def test_cache_hit_semantics_unchanged():
    """Regression guard: hit/miss and TTL boundaries behave exactly as before."""

    async def scenario():
        now = [0.0]
        store: dict[str, tuple[float, int]] = {}
        hits: list[bool] = []
        cache: TtlCache[str, int] = TtlCache(store, clock=lambda: now[0], record_hit=hits.append)
        calls = [0]

        async def fetch() -> int:
            calls[0] += 1
            return calls[0]

        first = await cache.get_or_fetch("k", fetch, ttl=10.0)
        now[0] = 5.0
        second = await cache.get_or_fetch("k", fetch, ttl=10.0)
        now[0] = 10.0  # exactly at TTL counts as expired
        third = await cache.get_or_fetch("k", fetch, ttl=10.0)
        return first, second, third, hits, calls[0]

    first, second, third, hits, calls = asyncio.run(scenario())
    assert (first, second, third) == (1, 1, 2)
    assert hits == [False, True, False]
    assert calls == 2
