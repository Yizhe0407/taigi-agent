"""Lifecycle contract for the app-owned shared HTTP client."""

from __future__ import annotations

import asyncio

import pytest

from providers.http import _SharedHttpClientOwner


class _FakeClient:
    def __init__(self) -> None:
        self.is_closed = False
        self.close_attempts = 0
        self.close_started = asyncio.Event()
        self.allow_close = asyncio.Event()
        self.allow_close.set()
        self.failures_remaining = 0

    async def aclose(self) -> None:
        self.close_attempts += 1
        self.close_started.set()
        await self.allow_close.wait()
        if self.failures_remaining:
            self.failures_remaining -= 1
            raise RuntimeError("close failed")
        self.is_closed = True


def test_closed_generation_cannot_lazily_resurrect_a_client() -> None:
    async def run() -> None:
        clients: list[_FakeClient] = []

        def factory() -> _FakeClient:
            client = _FakeClient()
            clients.append(client)
            return client

        owner = _SharedHttpClientOwner(factory)  # type: ignore[arg-type]
        assert owner.get() is owner.get()

        await owner.aclose()

        assert owner.closed
        assert clients[0].is_closed
        assert clients[0].close_attempts == 1
        with pytest.raises(RuntimeError, match="shutting down"):
            owner.get()
        assert len(clients) == 1

    asyncio.run(run())


def test_borrower_closed_client_is_a_terminal_generation_violation() -> None:
    async def run() -> None:
        clients: list[_FakeClient] = []

        def factory() -> _FakeClient:
            client = _FakeClient()
            clients.append(client)
            return client

        owner = _SharedHttpClientOwner(factory)  # type: ignore[arg-type]
        client = owner.get()
        await client.aclose()

        for _ in range(2):
            with pytest.raises(RuntimeError, match="closed outside its lifecycle owner"):
                owner.get()

        assert len(clients) == 1
        assert not owner.has_client
        assert not owner.closing

        await owner.aclose()

        assert owner.closed
        assert client.close_attempts == 1
        assert len(clients) == 1

    asyncio.run(run())


def test_concurrent_and_cancelled_waiters_join_one_physical_close() -> None:
    async def run() -> None:
        client = _FakeClient()
        client.allow_close.clear()
        owner = _SharedHttpClientOwner(lambda: client)  # type: ignore[arg-type]
        owner.get()

        first = asyncio.create_task(owner.aclose())
        await client.close_started.wait()
        second = asyncio.create_task(owner.aclose())
        first.cancel()

        client.allow_close.set()
        with pytest.raises(asyncio.CancelledError):
            await first
        await second

        assert owner.closed
        assert client.close_attempts == 1

    asyncio.run(run())


def test_failed_close_retains_debt_and_retries_without_reopening_gate() -> None:
    async def run() -> None:
        client = _FakeClient()
        client.failures_remaining = 1
        owner = _SharedHttpClientOwner(lambda: client)  # type: ignore[arg-type]
        owner.get()

        with pytest.raises(RuntimeError, match="close failed"):
            await owner.aclose()

        assert owner.closing
        assert not owner.closed
        assert owner.has_client
        with pytest.raises(RuntimeError, match="shutting down"):
            owner.get()

        await owner.aclose()

        assert owner.closed
        assert not owner.has_client
        assert client.is_closed
        assert client.close_attempts == 2

    asyncio.run(run())
