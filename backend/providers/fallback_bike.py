"""Provider-neutral composition for public bike providers."""

from __future__ import annotations

import logging
from collections.abc import Sequence

from providers.bike import BikeProvider, BikeProviderApiError, BikeStation
from telemetry import get_telemetry

_log = logging.getLogger(__name__)


def _provider_name(provider: BikeProvider) -> str:
    name = getattr(provider, "name", None)
    return name if isinstance(name, str) and name else type(provider).__name__


class FallbackBikeProvider:
    """Try an ordered list of providers without knowing their implementations.

    Only a non-empty snapshot ends the search.  A provider that raises, and a
    provider that answers with no stations at all, are both unusable for a
    region known to have bike stations, so this composition moves on to the
    next source instead of publishing an empty map.
    """

    name = "fallback"

    def __init__(self, providers: Sequence[BikeProvider]) -> None:
        if not providers:
            raise ValueError("at least one bike provider is required")
        self._providers = tuple(providers)

    @property
    def providers(self) -> tuple[BikeProvider, ...]:
        return self._providers

    async def fetch_stations(self) -> tuple[BikeStation, ...]:
        failures: list[tuple[str, Exception]] = []
        for index, provider in enumerate(self._providers):
            name = _provider_name(provider)
            try:
                stations = tuple(await provider.fetch_stations())
            except Exception as error:  # noqa: BLE001 — provider boundary must isolate upstream failures
                failures.append((name, error))
                _log.warning("bike provider %s failed: %s", name, error)
                self._record_miss(index)
                continue

            if not stations:
                failures.append((name, BikeProviderApiError("returned no stations")))
                _log.warning("bike provider %s returned no stations", name)
                self._record_miss(index)
                continue

            get_telemetry().record_provider_fallback(
                operation="bike.stations",
                outcome="primary_hit" if index == 0 else "fallback_hit",
            )
            return stations

        detail = "; ".join(f"{name}: {error}" for name, error in failures)
        get_telemetry().record_provider_fallback(operation="bike.stations", outcome="all_failed")
        raise BikeProviderApiError(f"all bike providers failed: {detail}") from failures[-1][1]

    @staticmethod
    def _record_miss(index: int) -> None:
        get_telemetry().record_provider_fallback(
            operation="bike.stations",
            outcome="primary_failed" if index == 0 else "fallback_failed",
        )
