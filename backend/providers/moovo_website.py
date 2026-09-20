"""MOOVO official city-map website provider.

The operator's city-map page renders station rows server-side.  This adapter
uses only the public station table and translates it into the neutral bike
contract.  It deliberately leaves fields absent from that table as ``None``.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser

import httpx

from providers.bike import BikeProviderApiError, BikeStation
from providers.http import get_http_client

_DEFAULT_URL = "https://www.ridemoovo.com/city_map_Yunlin"
_REQUEST_TIMEOUT_SECONDS = 20.0


def _parse_float(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _parse_non_negative_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return max(0, number)


def _classes(attrs: list[tuple[str, str | None]]) -> set[str]:
    return set((dict(attrs).get("class") or "").split())


@dataclass
class _StationRow:
    cells: list[str]
    latitude: float | None
    longitude: float | None


class _StationTableParser(HTMLParser):
    """Extract rows from the public city-map table without a DOM dependency."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[_StationRow] = []
        self._row_depth = 0
        self._cell_depth = 0
        self._cell_text: list[str] | None = None
        self._cells: list[str] = []
        self._latitude: float | None = None
        self._longitude: float | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "div" and self._row_depth == 0 and "city-location-row-block" in _classes(attrs):
            self._row_depth = 1
            self._cell_depth = 0
            self._cell_text = None
            self._cells = []
            self._latitude = None
            self._longitude = None
            return

        if self._row_depth == 0:
            return

        if tag == "div":
            self._row_depth += 1
            if "city-location-td" in _classes(attrs):
                self._cell_depth = 1
                self._cell_text = []
            elif self._cell_depth:
                self._cell_depth += 1

        if tag == "a":
            attributes = dict(attrs)
            self._latitude = _parse_float(attributes.get("data-lat"))
            self._longitude = _parse_float(attributes.get("data-lon"))

    def handle_endtag(self, tag: str) -> None:
        if self._row_depth == 0 or tag != "div":
            return

        if self._cell_depth:
            self._cell_depth -= 1
            if self._cell_depth == 0:
                text = " ".join("".join(self._cell_text or []).split())
                self._cells.append(text)
                self._cell_text = None

        self._row_depth -= 1
        if self._row_depth == 0:
            self.rows.append(_StationRow(self._cells, self._latitude, self._longitude))
            self._cell_depth = 0
            self._cell_text = None

    def handle_data(self, data: str) -> None:
        if self._cell_depth and self._cell_text is not None:
            self._cell_text.append(data)


def _station_uid(name: str, latitude: float, longitude: float) -> str:
    identity = f"{name.strip()}|{latitude:.7f}|{longitude:.7f}".encode()
    return f"moovo-web-{hashlib.sha256(identity).hexdigest()[:20]}"


def parse_station_page(html: str, *, fetched_at: datetime | None = None) -> tuple[BikeStation, ...]:
    """Parse the official city-map HTML into normalized station snapshots."""
    parser = _StationTableParser()
    parser.feed(html)
    parser.close()

    observed_at = fetched_at or datetime.now(UTC)
    stations: list[BikeStation] = []
    seen: set[str] = set()
    for row in parser.rows:
        if len(row.cells) < 3 or row.latitude is None or row.longitude is None:
            continue
        name = row.cells[1].strip()
        available_rent_bikes = _parse_non_negative_int(row.cells[2])
        if not name or available_rent_bikes is None:
            continue

        station_uid = _station_uid(name, row.latitude, row.longitude)
        if station_uid in seen:
            continue
        seen.add(station_uid)
        stations.append(
            BikeStation(
                station_uid=station_uid,
                station_id=None,
                name=name,
                latitude=row.latitude,
                longitude=row.longitude,
                bike_capacity=None,
                available_rent_bikes=available_rent_bikes,
                available_return_bikes=None,
                service_status=None,
                update_time=observed_at,
                provider="moovo_web",
            )
        )

    if not stations:
        raise BikeProviderApiError("MOOVO website returned no usable station rows")
    stations.sort(key=lambda station: (station.name, station.station_uid))
    return tuple(stations)


class MoovoWebsiteProvider:
    """Read station availability from the official MOOVO city-map page."""

    name = "moovo_web"

    def __init__(
        self,
        *,
        url: str = _DEFAULT_URL,
        timeout: float = _REQUEST_TIMEOUT_SECONDS,
        http_client_factory: Callable[[], httpx.AsyncClient] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._url = url
        self._timeout = timeout
        self._http_client_factory = http_client_factory or get_http_client
        self._clock = clock or (lambda: datetime.now(UTC))

    async def fetch_stations(self) -> tuple[BikeStation, ...]:
        client = self._http_client_factory()
        try:
            response = await client.get(
                self._url,
                headers={"Accept": "text/html", "User-Agent": "taigi-agent bike provider"},
                timeout=self._timeout,
            )
            response.raise_for_status()
            html = response.text
        except httpx.HTTPError as error:
            raise BikeProviderApiError(f"MOOVO website request failed: {error}") from error
        except UnicodeError as error:
            raise BikeProviderApiError("MOOVO website response is not valid text") from error

        return parse_station_page(html, fetched_at=self._clock())
