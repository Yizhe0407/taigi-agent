"""Composition root and runtime registry for bike providers.

The service layer asks this module for a provider but never imports a concrete
upstream adapter.  New providers register a factory here and can be selected
through ``BIKE_PROVIDER_ORDER`` or ``configure_providers``.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager

from providers.bike import BikeProvider, BikeProviderConfigError
from providers.fallback_bike import FallbackBikeProvider
from providers.moovo_website import MoovoWebsiteProvider
from providers.tdx_bike import TdxBikeProvider

ProviderFactory = Callable[[], BikeProvider]

_PROVIDER_FACTORIES: dict[str, ProviderFactory] = {
    "tdx": TdxBikeProvider,
    "moovo_web": MoovoWebsiteProvider,
}
_provider: BikeProvider | None = None
_DEFAULT_PROVIDER_ORDER = ("tdx", "moovo_web")


def register_provider(name: str, factory: ProviderFactory) -> None:
    """Register or replace a named provider factory before composition."""
    normalized = name.strip()
    if not normalized:
        raise ValueError("provider name must not be empty")
    _PROVIDER_FACTORIES[normalized] = factory


def registered_provider_names() -> tuple[str, ...]:
    return tuple(_PROVIDER_FACTORIES)


def _configured_order() -> tuple[str, ...]:
    raw = os.getenv("BIKE_PROVIDER_ORDER", "")
    names = tuple(name.strip() for name in raw.split(",") if name.strip()) if raw else _DEFAULT_PROVIDER_ORDER
    if not names:
        raise BikeProviderConfigError("BIKE_PROVIDER_ORDER must contain at least one provider")
    return names


def configure_providers(names: Sequence[str]) -> BikeProvider:
    """Build and install an ordered fallback chain from registered factories."""
    if not names:
        raise BikeProviderConfigError("at least one bike provider is required")
    providers: list[BikeProvider] = []
    for name in names:
        factory = _PROVIDER_FACTORIES.get(name)
        if factory is None:
            available = ", ".join(registered_provider_names())
            raise BikeProviderConfigError(f"unknown bike provider {name!r}; available: {available}")
        providers.append(factory())

    provider: BikeProvider = providers[0] if len(providers) == 1 else FallbackBikeProvider(providers)
    set_provider(provider)
    return provider


def get_provider() -> BikeProvider:
    global _provider
    if _provider is None:
        _provider = configure_providers(_configured_order())
    return _provider


def set_provider(provider: BikeProvider) -> None:
    global _provider
    _provider = provider


@contextmanager
def provider_override(provider: BikeProvider) -> Iterator[BikeProvider]:
    previous = _provider
    set_provider(provider)
    try:
        yield provider
    finally:
        if previous is None:
            reset_provider()
        else:
            set_provider(previous)


def reset_provider() -> None:
    global _provider
    _provider = None
