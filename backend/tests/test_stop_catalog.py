import json

from tools import stop_catalog


def test_load_stop_catalog_matches_exact_names_and_suggestions(tmp_path, monkeypatch):
    path = tmp_path / "stops.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "stops": [
                    {
                        "id": "THB249193",
                        "name": "虎尾",
                        "latitude": 23.709539,
                        "longitude": 120.434305,
                    },
                    {
                        "id": "THB248875",
                        "name": "虎尾圓環",
                        "latitude": 23.71019,
                        "longitude": 120.43696,
                    },
                    {
                        "id": "THB248876",
                        "name": "虎尾圓環",
                        "latitude": 23.7102,
                        "longitude": 120.4369,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("YUNLIN_STOP_INDEX_PATH", str(path))
    stop_catalog._load_catalog.cache_clear()

    catalog = stop_catalog.load_stop_catalog()

    assert [stop.stop_id for stop in catalog.exact("虎尾")] == ["THB249193"]
    assert catalog.suggest("虎") == ("虎尾", "虎尾圓環")
