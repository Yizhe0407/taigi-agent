import json

import pytest

from services import stop_catalog


def _write_catalog(path, stops, *, schema_version=stop_catalog._SCHEMA_VERSION):
    path.write_text(
        json.dumps({"schema_version": schema_version, "stops": stops}),
        encoding="utf-8",
    )


def _use_catalog(monkeypatch, path) -> None:
    monkeypatch.setenv("YUNLIN_STOP_INDEX_PATH", str(path))
    stop_catalog._load_catalog.cache_clear()


def test_exact_returns_all_records_with_matching_name(tmp_path, monkeypatch):
    path = tmp_path / "stops.json"
    _write_catalog(
        path,
        [
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
    )
    _use_catalog(monkeypatch, path)

    matches = stop_catalog.load_stop_catalog().exact("虎尾圓環")

    assert [stop.stop_id for stop in matches] == ["THB248875", "THB248876"]


def test_exact_returns_empty_when_name_not_present(tmp_path, monkeypatch):
    path = tmp_path / "stops.json"
    _write_catalog(
        path,
        [
            {
                "id": "THB249193",
                "name": "虎尾",
                "latitude": 23.709539,
                "longitude": 120.434305,
            }
        ],
    )
    _use_catalog(monkeypatch, path)

    assert stop_catalog.load_stop_catalog().exact("不存在") == []


def test_load_stop_catalog_raises_when_file_missing(tmp_path, monkeypatch):
    _use_catalog(monkeypatch, tmp_path / "missing.json")

    with pytest.raises(stop_catalog.StopCatalogError, match="找不到"):
        stop_catalog.load_stop_catalog()


def test_load_stop_catalog_raises_for_invalid_json(tmp_path, monkeypatch):
    path = tmp_path / "stops.json"
    path.write_text("{not json", encoding="utf-8")
    _use_catalog(monkeypatch, path)

    with pytest.raises(stop_catalog.StopCatalogError, match="無法讀取"):
        stop_catalog.load_stop_catalog()


def test_load_stop_catalog_rejects_unsupported_schema_version(tmp_path, monkeypatch):
    path = tmp_path / "stops.json"
    _write_catalog(path, [], schema_version=stop_catalog._SCHEMA_VERSION + 1)
    _use_catalog(monkeypatch, path)

    with pytest.raises(stop_catalog.StopCatalogError, match="schema version"):
        stop_catalog.load_stop_catalog()


def test_load_stop_catalog_rejects_payload_without_stops_list(tmp_path, monkeypatch):
    path = tmp_path / "stops.json"
    path.write_text(
        json.dumps({"schema_version": stop_catalog._SCHEMA_VERSION}),
        encoding="utf-8",
    )
    _use_catalog(monkeypatch, path)

    with pytest.raises(stop_catalog.StopCatalogError, match="missing stops"):
        stop_catalog.load_stop_catalog()


def test_load_stop_catalog_rejects_stop_with_missing_fields(tmp_path, monkeypatch):
    path = tmp_path / "stops.json"
    _write_catalog(
        path,
        [{"id": "THB249193", "name": "虎尾", "latitude": 23.7}],
    )
    _use_catalog(monkeypatch, path)

    with pytest.raises(stop_catalog.StopCatalogError, match="missing stop fields"):
        stop_catalog.load_stop_catalog()


def test_catalog_path_defaults_when_env_unset(monkeypatch):
    monkeypatch.delenv("YUNLIN_STOP_INDEX_PATH", raising=False)

    assert stop_catalog._catalog_path() == stop_catalog._DEFAULT_CATALOG_PATH


def test_catalog_path_joins_relative_env_to_backend_root(monkeypatch):
    monkeypatch.setenv("YUNLIN_STOP_INDEX_PATH", "otp/data/custom.json")

    assert stop_catalog._catalog_path() == stop_catalog._BACKEND_ROOT / "otp/data/custom.json"


def test_catalog_path_uses_absolute_env_as_is(tmp_path, monkeypatch):
    absolute = tmp_path / "custom.json"
    monkeypatch.setenv("YUNLIN_STOP_INDEX_PATH", str(absolute))

    assert stop_catalog._catalog_path() == absolute
