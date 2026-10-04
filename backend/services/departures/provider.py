"""Composition root for the bus provider.

TDX is the only bus data source: it is the official, credentialed API, while
the scraped sources it used to be chained with (TaiwanBus, Yunlin ebus) kept
getting IP-blocked or going down. The service layer still talks only to the
provider-neutral ``BusProvider`` Protocol, so a test (or a future source) can
swap the instance via ``set_provider`` / ``provider_override``.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

from providers.bus import BusProvider
from providers.tdx_bus import TdxBusProvider

_provider: BusProvider | None = None


def _make_tdx() -> BusProvider:
    return TdxBusProvider(
        client_id=os.environ.get("TDX_CLIENT_ID", ""),
        client_secret=os.environ.get("TDX_CLIENT_SECRET", ""),
    )


def get_provider() -> BusProvider:
    global _provider
    if _provider is None:
        _provider = _make_tdx()
    return _provider


def set_provider(provider: BusProvider) -> None:
    """Swap the active `BusProvider`.

    Prefer `provider_override()` from test / scoped code so the previous
    instance is restored automatically.
    """
    global _provider
    _provider = provider


@contextmanager
def provider_override(provider: BusProvider) -> Iterator[BusProvider]:
    """Scope a temporary BusProvider; restore the previous one on exit."""
    global _provider
    previous = _provider
    set_provider(provider)
    try:
        yield provider
    finally:
        _provider = previous


def reset_provider() -> None:
    global _provider
    _provider = None
