from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from providers.moovo_website import MoovoWebsiteProvider, parse_station_page

_HTML = """
<div class="city-location-row-block">
  <div class="city-location-row">
    <div class="city-location-td"><span>1</span></div>
    <div class="city-location-td"><span>斗六棒球場</span></div>
    <div class="city-location-td"><span>3</span></div>
    <div class="city-location-td">
      <a data-lat="23.715678" data-lon="120.535596"></a>
    </div>
  </div>
</div>
<div class="city-location-row-block">
  <div class="city-location-row">
    <div class="city-location-td"><span>2</span></div>
    <div class="city-location-td"><span>沒有座標</span></div>
    <div class="city-location-td"><span>7</span></div>
  </div>
</div>
<div class="city-location-row-block">
  <div class="city-location-row">
    <div class="city-location-td"><span>3</span></div>
    <div class="city-location-td"><span>斗六國中</span></div>
    <div class="city-location-td"><span>0</span></div>
    <div class="city-location-td">
      <a data-lat="23.712298" data-lon="120.551390"></a>
    </div>
  </div>
</div>
"""


def test_parse_station_page_normalizes_station_rows():
    fetched_at = datetime(2026, 9, 20, 3, 0, tzinfo=UTC)

    stations = parse_station_page(_HTML, fetched_at=fetched_at)

    assert [station.name for station in stations] == ["斗六國中", "斗六棒球場"]
    baseball = next(station for station in stations if station.name == "斗六棒球場")
    assert baseball.available_rent_bikes == 3
    assert baseball.latitude == 23.715678
    assert baseball.longitude == 120.535596
    assert baseball.provider == "moovo_web"
    assert baseball.bike_capacity is None
    assert baseball.available_return_bikes is None
    assert baseball.service_status is None
    assert baseball.update_time == fetched_at


def test_parse_station_page_rejects_html_without_usable_rows():
    import pytest

    from providers.bike import BikeProviderApiError

    with pytest.raises(BikeProviderApiError, match="no usable station rows"):
        parse_station_page("<html><body>changed</body></html>")


def test_website_provider_fetches_and_parses_html(monkeypatch):
    class FakeResponse:
        text = _HTML

        def raise_for_status(self):
            return None

    class FakeClient:
        async def get(self, url, **kwargs):
            assert url.endswith("city_map_Yunlin")
            return FakeResponse()

    provider = MoovoWebsiteProvider(http_client_factory=lambda: FakeClient())

    stations = asyncio.run(provider.fetch_stations())

    assert len(stations) == 2
    assert all(station.provider == "moovo_web" for station in stations)
