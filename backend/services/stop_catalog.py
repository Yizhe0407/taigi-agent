"""Generated GTFS stop catalog used by Yunlin route planning."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

from providers.otp import Coordinate

_BACKEND_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_CATALOG_PATH = _BACKEND_ROOT / "otp/data/yunlin-stop-index.json"
_SCHEMA_VERSION = 1


class StopCatalogError(RuntimeError):
    """Raised when the generated stop catalog cannot be used."""


@dataclass(frozen=True)
class StopRecord:
    stop_id: str
    name: str
    coordinate: Coordinate


@dataclass(frozen=True)
class StopCatalog:
    stops: tuple[StopRecord, ...]

    def exact(self, name: str) -> list[StopRecord]:
        return [stop for stop in self.stops if stop.name == name]


def _catalog_path() -> Path:
    configured = os.getenv("YUNLIN_STOP_INDEX_PATH")
    if not configured:
        return _DEFAULT_CATALOG_PATH

    path = Path(configured)
    return path if path.is_absolute() else _BACKEND_ROOT / path


def _parse_stop(data: Any) -> StopRecord:
    if not isinstance(data, dict):
        raise StopCatalogError("stop index contains invalid stop data")

    stop_id = data.get("id")
    name = data.get("name")
    latitude = data.get("latitude")
    longitude = data.get("longitude")
    if (
        not isinstance(stop_id, str)
        or not isinstance(name, str)
        or not isinstance(latitude, int | float)
        or not isinstance(longitude, int | float)
    ):
        raise StopCatalogError("stop index is missing stop fields")

    return StopRecord(
        stop_id=stop_id,
        name=name,
        coordinate=Coordinate(
            latitude=float(latitude),
            longitude=float(longitude),
        ),
    )


@cache
def _load_catalog(path: str) -> StopCatalog:
    catalog_path = Path(path)
    try:
        payload = json.loads(catalog_path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise StopCatalogError(f"找不到 {catalog_path}") from error
    except (OSError, json.JSONDecodeError) as error:
        raise StopCatalogError(f"無法讀取 {catalog_path}: {error}") from error

    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != _SCHEMA_VERSION
    ):
        raise StopCatalogError("stop index schema version is unsupported")

    stops = payload.get("stops")
    if not isinstance(stops, list):
        raise StopCatalogError("stop index is missing stops")
    return StopCatalog(tuple(_parse_stop(stop) for stop in stops))


def load_stop_catalog() -> StopCatalog:
    """Load the generated Yunlin GTFS stop catalog."""
    return _load_catalog(str(_catalog_path()))
