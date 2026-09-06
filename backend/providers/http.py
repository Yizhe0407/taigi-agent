"""App-lifespan owner for the shared ``httpx.AsyncClient``.

Every upstream provider borrows the same connection pool, but none of those
providers owns it.  This module is the sole lifecycle authority: construction
crosses a permanent acquisition gate, the client remains strongly retained
until close succeeds, concurrent/cancelled shutdown waiters join one physical
close, and a closed lifespan cannot lazily resurrect a replacement client.

``api._lifespan`` explicitly opens a fresh generation with
:func:`startup_http_client` and closes it after every client task/provider has
stopped.  Non-ASGI entrypoints may use the initial generation lazily, but after
calling :func:`aclose_http_client` they must explicitly start a new generation.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import httpx

from async_lifecycle import AsyncResourceOwner, OwnedAsyncResource

_DEFAULT_TIMEOUT = 20.0


class _SharedHttpClientOwner:
    """Own exactly one loop-bound HTTP pool for one application generation."""

    def __init__(
        self,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
    ) -> None:
        self._client_factory = client_factory or (lambda: httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT))
        self._resources: AsyncResourceOwner[httpx.AsyncClient] = AsyncResourceOwner("shared HTTP client")
        self._entry: OwnedAsyncResource[httpx.AsyncClient] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def closing(self) -> bool:
        return self._resources.closing

    @property
    def closed(self) -> bool:
        return self._resources.closed

    @property
    def has_client(self) -> bool:
        return self._entry is not None and self._resources.owns(self._entry)

    @staticmethod
    async def _close_client(client: httpx.AsyncClient) -> None:
        if not client.is_closed:
            await client.aclose()

    def get(self) -> httpx.AsyncClient:
        """Return the generation's client without crossing a closed gate."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError as error:
            raise RuntimeError("The shared HTTP client must be acquired inside its owning event loop") from error

        if self._resources.closing:
            raise RuntimeError("The shared HTTP client owner is shutting down")

        entry = self._entry
        if entry is not None and entry.resource.is_closed:
            # The lifespan owner is the only authority allowed to close this pool.
            # Retire the physical resource debt, but retain ``_entry`` as this
            # generation's terminal tombstone: silently constructing a replacement
            # would let a borrower-created ownership violation resurrect state.
            self._resources.retire_closed(entry)
            raise RuntimeError("The shared HTTP client was closed outside its lifecycle owner")

        if entry is not None:
            if self._loop is not loop:
                raise RuntimeError("The shared HTTP client cannot be used from a different event loop")
            return entry.resource

        acquisition = self._resources.begin_acquisition()
        try:
            client = self._client_factory()
        except BaseException:
            self._resources.abort_acquisition(acquisition)
            raise

        # No await separates construction from adoption, so shutdown cannot take
        # a snapshot in between and strand an unowned client.
        entry = self._resources.finish_acquisition(
            acquisition,
            client,
            self._close_client,
        )
        self._entry = entry
        self._loop = loop
        return client

    async def aclose(self) -> None:
        """Permanently close this generation, retaining failed close debt."""
        await self._resources.aclose()
        if self._resources.closed:
            self._entry = None
            self._loop = None


_owner = _SharedHttpClientOwner()


def startup_http_client() -> None:
    """Install a fresh owner only after the previous generation fully closed."""
    global _owner
    if _owner.closed:
        _owner = _SharedHttpClientOwner()
    elif _owner.closing:
        raise RuntimeError("Cannot start a new shared HTTP client generation before shutdown succeeds")


def get_http_client() -> httpx.AsyncClient:
    """Borrow the current generation's shared client."""
    return _owner.get()


async def aclose_http_client() -> None:
    """Close the current generation; failed closes remain retryable."""
    await _owner.aclose()
