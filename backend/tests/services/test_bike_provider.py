from __future__ import annotations

from providers.bike import BikeStation
from providers.fallback_bike import FallbackBikeProvider
from services import bike


class _CustomProvider:
    name = "custom"

    async def fetch_stations(self) -> tuple[BikeStation, ...]:
        return ()


def test_configure_providers_can_select_one_registered_provider():
    bike.register_provider("test_custom", _CustomProvider)
    try:
        provider = bike.configure_providers(["test_custom"])
        assert isinstance(provider, _CustomProvider)
        assert bike.get_provider() is provider
    finally:
        bike.reset_provider()


def test_configure_providers_composes_any_ordered_provider_list():
    bike.register_provider("test_custom", _CustomProvider)
    try:
        provider = bike.configure_providers(["test_custom", "test_custom"])
        assert isinstance(provider, FallbackBikeProvider)
        assert [getattr(item, "name") for item in provider.providers] == ["custom", "custom"]
    finally:
        bike.reset_provider()
