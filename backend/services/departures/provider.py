from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from providers.bus import BusProvider
from providers.yunlin_ebus import YunlinEbusProvider

_log = logging.getLogger(__name__)
_provider: BusProvider = YunlinEbusProvider()


def get_provider() -> BusProvider:
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
    """Scope a temporary BusProvider; restore the previous one on exit.

    Use this from tests and short-lived swaps so a thrown exception or an
    early return cannot leave the module pinned to a fake provider.
    """
    previous = _provider
    set_provider(provider)
    try:
        yield provider
    finally:
        set_provider(previous)
