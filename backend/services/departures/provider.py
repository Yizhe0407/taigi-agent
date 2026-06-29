from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager

from providers.bus import BusProvider

_log = logging.getLogger(__name__)
_provider: BusProvider | None = None


def _make_default_provider() -> BusProvider:
    from providers.ebus import EbusBusProvider
    from providers.hybrid import HybridBusProvider
    from providers.tdx_bus import TdxBusProvider

    tdx = TdxBusProvider(
        client_id=os.environ.get("TDX_CLIENT_ID", ""),
        client_secret=os.environ.get("TDX_CLIENT_SECRET", ""),
    )
    ebus = EbusBusProvider()
    return HybridBusProvider(ebus=ebus, tdx=tdx)


def get_provider() -> BusProvider:
    global _provider
    if _provider is None:
        _provider = _make_default_provider()
    return _provider


def set_provider(provider: BusProvider) -> None:
    """Swap the active `BusProvider` (boot-time wiring, multi-region rollouts).

    Prefer `provider_override()` from test / scoped code so the previous
    instance is restored automatically.
    """
    global _provider
    _provider = provider


@contextmanager
def provider_override(provider: BusProvider) -> Iterator[BusProvider]:
    """Scope a temporary BusProvider; restore the previous one on exit."""
    previous = _provider
    set_provider(provider)
    try:
        yield provider
    finally:
        set_provider(previous)
