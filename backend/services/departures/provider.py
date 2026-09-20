"""Composition root and runtime registry for bus providers.

The service layer asks this module for a provider but never imports a concrete
upstream adapter.  New providers register a factory here and can be selected
through ``BUS_PROVIDER_ORDER`` or ``configure_providers``.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

from providers.bus import BusProvider, BusProviderConfigError
from providers.ebus import EbusBusProvider
from providers.fallback import FallbackBusProvider
from providers.taiwan_bus import TaiwanBusProvider
from providers.tdx_bus import TdxBusProvider

ProviderFactory = Callable[[], BusProvider]


def _make_tdx() -> BusProvider:
    return TdxBusProvider(
        client_id=os.environ.get("TDX_CLIENT_ID", ""),
        client_secret=os.environ.get("TDX_CLIENT_SECRET", ""),
    )


def _make_ebus() -> BusProvider:
    # The scan that builds this index costs ~20 concurrent upstream requests,
    # so it is persisted across restarts.
    route_index_path = Path(
        os.getenv(
            "EBUS_ROUTE_INDEX_PATH",
            str(Path(__file__).resolve().parents[2] / ".agent_state/ebus-route-index.json"),
        )
    )
    return EbusBusProvider(route_index_path=route_index_path)


_PROVIDER_FACTORIES: dict[str, ProviderFactory] = {
    "taiwanbus": TaiwanBusProvider,
    "tdx": _make_tdx,
    "ebus": _make_ebus,
}
_provider: BusProvider | None = None
_DEFAULT_PROVIDER_ORDER = ("taiwanbus", "tdx")


def register_provider(name: str, factory: ProviderFactory) -> None:
    """Register or replace a named provider factory before composition."""
    normalized = name.strip()
    if not normalized:
        raise ValueError("provider name must not be empty")
    _PROVIDER_FACTORIES[normalized] = factory


def registered_provider_names() -> tuple[str, ...]:
    return tuple(_PROVIDER_FACTORIES)


def _configured_order() -> tuple[str, ...]:
    raw = os.getenv("BUS_PROVIDER_ORDER", "")
    names = tuple(name.strip() for name in raw.split(",") if name.strip()) if raw else _DEFAULT_PROVIDER_ORDER
    if not names:
        raise BusProviderConfigError("BUS_PROVIDER_ORDER must contain at least one provider")
    return names


def configure_providers(names: Sequence[str]) -> BusProvider:
    """Build and install an ordered fallback chain from registered factories."""
    if not names:
        raise BusProviderConfigError("at least one bus provider is required")
    providers: list[BusProvider] = []
    for name in names:
        factory = _PROVIDER_FACTORIES.get(name)
        if factory is None:
            available = ", ".join(registered_provider_names())
            raise BusProviderConfigError(f"unknown bus provider {name!r}; available: {available}")
        providers.append(factory())

    provider: BusProvider = providers[0] if len(providers) == 1 else FallbackBusProvider(providers)
    set_provider(provider)
    return provider


def get_provider() -> BusProvider:
    global _provider
    if _provider is None:
        _provider = configure_providers(_configured_order())
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
