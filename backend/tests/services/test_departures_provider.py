"""Tests for the bus-provider composition root and registry."""

from __future__ import annotations

import pytest

from providers.bus import BusProviderConfigError
from providers.fallback import FallbackBusProvider
from services.departures import provider as provider_module


@pytest.fixture(autouse=True)
def _restore_registry():
    saved_factories = dict(provider_module._PROVIDER_FACTORIES)
    saved_provider = provider_module._provider
    yield
    provider_module._PROVIDER_FACTORIES.clear()
    provider_module._PROVIDER_FACTORIES.update(saved_factories)
    provider_module._provider = saved_provider


class _Fake:
    def __init__(self, name: str) -> None:
        self.name = name


def test_default_order_is_taiwanbus_then_tdx(monkeypatch):
    monkeypatch.delenv("BUS_PROVIDER_ORDER", raising=False)
    provider_module._provider = None
    built: list[str] = []
    for name in ("taiwanbus", "tdx", "ebus"):
        provider_module.register_provider(name, lambda name=name: (built.append(name), _Fake(name))[1])

    provider = provider_module.get_provider()

    assert built == ["taiwanbus", "tdx"]
    assert isinstance(provider, FallbackBusProvider)
    assert [p.name for p in provider.providers] == ["taiwanbus", "tdx"]


def test_env_order_selects_and_reorders_providers(monkeypatch):
    monkeypatch.setenv("BUS_PROVIDER_ORDER", "ebus, tdx")
    provider_module._provider = None
    for name in ("taiwanbus", "tdx", "ebus"):
        provider_module.register_provider(name, lambda name=name: _Fake(name))

    provider = provider_module.get_provider()

    assert [p.name for p in provider.providers] == ["ebus", "tdx"]


def test_single_provider_order_skips_the_fallback_wrapper():
    provider_module.register_provider("only", lambda: _Fake("only"))

    provider = provider_module.configure_providers(["only"])

    assert not isinstance(provider, FallbackBusProvider)
    assert provider.name == "only"


def test_unknown_provider_name_lists_the_registered_ones():
    with pytest.raises(BusProviderConfigError, match="unknown bus provider 'nope'"):
        provider_module.configure_providers(["nope"])


def test_empty_order_is_rejected(monkeypatch):
    monkeypatch.setenv("BUS_PROVIDER_ORDER", " , ")
    provider_module._provider = None

    with pytest.raises(BusProviderConfigError, match="at least one provider"):
        provider_module.get_provider()


def test_a_new_provider_can_be_registered_and_selected(monkeypatch):
    monkeypatch.setenv("BUS_PROVIDER_ORDER", "custom")
    provider_module._provider = None
    provider_module.register_provider("custom", lambda: _Fake("custom"))

    assert "custom" in provider_module.registered_provider_names()
    assert provider_module.get_provider().name == "custom"


def test_provider_override_restores_the_previous_instance():
    provider_module.set_provider(_Fake("base"))
    with provider_module.provider_override(_Fake("temp")) as temporary:
        assert provider_module.get_provider() is temporary
    assert provider_module.get_provider().name == "base"


def test_reset_provider_forces_recomposition(monkeypatch):
    monkeypatch.setenv("BUS_PROVIDER_ORDER", "custom")
    provider_module.register_provider("custom", lambda: _Fake("custom"))
    provider_module.set_provider(_Fake("stale"))

    provider_module.reset_provider()

    assert provider_module.get_provider().name == "custom"
