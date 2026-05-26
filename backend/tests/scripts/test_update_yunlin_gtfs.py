from __future__ import annotations

import csv
import io
import json
import zipfile
from pathlib import Path

from scripts.update_yunlin_gtfs import filter_yunlin_gtfs, write_stop_index


def _write_table(
    archive: zipfile.ZipFile,
    name: str,
    fields: list[str],
    rows: list[dict[str, str]],
) -> None:
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    archive.writestr(name, output.getvalue())


def _read_rows(archive: zipfile.ZipFile, name: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(archive.read(name).decode())))


def _build_source(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        _write_table(
            archive,
            "agency.txt",
            ["agency_id", "agency_name"],
            [
                {"agency_id": "YUN_34", "agency_name": "Yunlin"},
                {"agency_id": "CHA_34", "agency_name": "Other"},
                {"agency_id": "THB_21", "agency_name": "Intercity"},
                {"agency_id": "THSR", "agency_name": "Rail"},
            ],
        )
        _write_table(
            archive,
            "routes.txt",
            ["route_id", "agency_id", "route_short_name", "route_type"],
            [
                {
                    "route_id": "YUN_ROUTE",
                    "agency_id": "YUN_34",
                    "route_short_name": "201",
                    "route_type": "3",
                },
                {
                    "route_id": "CHA_ROUTE",
                    "agency_id": "CHA_34",
                    "route_short_name": "1",
                    "route_type": "3",
                },
                {
                    "route_id": "THB_ROUTE",
                    "agency_id": "THB_21",
                    "route_short_name": "7120",
                    "route_type": "3",
                },
                {
                    "route_id": "RAIL_ROUTE",
                    "agency_id": "THSR",
                    "route_short_name": "Rail",
                    "route_type": "2",
                },
            ],
        )
        _write_table(
            archive,
            "trips.txt",
            ["route_id", "service_id", "trip_id", "shape_id"],
            [
                {
                    "route_id": "YUN_ROUTE",
                    "service_id": "YUN_SERVICE",
                    "trip_id": "YUN_TRIP",
                    "shape_id": "YUN_SHAPE",
                },
                {
                    "route_id": "YUN_ROUTE",
                    "service_id": "YUN_DUP",
                    "trip_id": "YUN_TRIP",
                    "shape_id": "YUN_SHAPE",
                },
                {
                    "route_id": "CHA_ROUTE",
                    "service_id": "CHA_SERVICE",
                    "trip_id": "CHA_TRIP",
                    "shape_id": "CHA_SHAPE",
                },
                {
                    "route_id": "THB_ROUTE",
                    "service_id": "THB_SERVICE",
                    "trip_id": "THB_TRIP",
                    "shape_id": "THB_SHAPE",
                },
                {
                    "route_id": "RAIL_ROUTE",
                    "service_id": "RAIL_SERVICE",
                    "trip_id": "RAIL_TRIP",
                    "shape_id": "RAIL_SHAPE",
                },
            ],
        )
        _write_table(
            archive,
            "stop_times.txt",
            ["trip_id", "stop_id"],
            [
                {"trip_id": "YUN_TRIP", "stop_id": "YUN_STOP"},
                {"trip_id": "YUN_TRIP", "stop_id": "YUN_STOP_2"},
                {"trip_id": "CHA_TRIP", "stop_id": "CHA_STOP"},
                {"trip_id": "THB_TRIP", "stop_id": "THB_STOP"},
                {"trip_id": "THB_TRIP", "stop_id": "THB_STOP_2"},
                {"trip_id": "RAIL_TRIP", "stop_id": "RAIL_STOP"},
                {"trip_id": "RAIL_TRIP", "stop_id": "RAIL_STOP_2"},
            ],
        )
        _write_table(
            archive,
            "stops.txt",
            ["stop_id", "stop_name", "stop_lat", "stop_lon", "level_id"],
            [
                {
                    "stop_id": "YUN_STOP",
                    "stop_name": "Yunlin Stop",
                    "stop_lat": "23.69",
                    "stop_lon": "120.53",
                    "level_id": "1",
                },
                {
                    "stop_id": "YUN_STOP_2",
                    "stop_name": "Yunlin Stop 2",
                    "stop_lat": "23.70",
                    "stop_lon": "120.54",
                    "level_id": "1",
                },
                {
                    "stop_id": "CHA_STOP",
                    "stop_name": "Other Stop",
                    "stop_lat": "25.03",
                    "stop_lon": "121.56",
                    "level_id": "1",
                },
                {
                    "stop_id": "THB_STOP",
                    "stop_name": "Intercity Stop",
                    "stop_lat": "23.70",
                    "stop_lon": "120.54",
                    "level_id": "1",
                },
                {
                    "stop_id": "THB_STOP_2",
                    "stop_name": "Intercity Stop 2",
                    "stop_lat": "23.71",
                    "stop_lon": "120.55",
                    "level_id": "1",
                },
                {
                    "stop_id": "RAIL_STOP",
                    "stop_name": "Rail Stop",
                    "stop_lat": "23.72",
                    "stop_lon": "120.56",
                    "level_id": "1",
                },
                {
                    "stop_id": "RAIL_STOP_2",
                    "stop_name": "Rail Stop 2",
                    "stop_lat": "23.73",
                    "stop_lon": "120.57",
                    "level_id": "1",
                },
            ],
        )
        _write_table(
            archive,
            "calendar.txt",
            ["service_id"],
            [
                {"service_id": "YUN_SERVICE"},
                {"service_id": "CHA_SERVICE"},
                {"service_id": "THB_SERVICE"},
                {"service_id": "RAIL_SERVICE"},
            ],
        )
        _write_table(
            archive,
            "calendar_dates.txt",
            ["service_id"],
            [
                {"service_id": "YUN_SERVICE"},
                {"service_id": "CHA_SERVICE"},
                {"service_id": "THB_SERVICE"},
                {"service_id": "RAIL_SERVICE"},
            ],
        )
        _write_table(
            archive,
            "shapes.txt",
            ["shape_id", "shape_pt_sequence"],
            [
                {"shape_id": "YUN_SHAPE", "shape_pt_sequence": "1"},
                {"shape_id": "CHA_SHAPE", "shape_pt_sequence": "1"},
                {"shape_id": "THB_SHAPE", "shape_pt_sequence": "1"},
                {"shape_id": "RAIL_SHAPE", "shape_pt_sequence": "1"},
            ],
        )
        archive.writestr("levels.txt", "level_id\n1\n")


def test_filter_yunlin_gtfs_keeps_yunlin_graph_inputs(tmp_path: Path) -> None:
    source = tmp_path / "tdx.zip"
    output = tmp_path / "yunlin.zip"
    index = tmp_path / "yunlin-stop-index.json"
    _build_source(source)
    planning_stop_ids = {"YUN_STOP", "YUN_STOP_2", "THB_STOP"}

    stats = filter_yunlin_gtfs(
        source,
        output,
        planning_stop_ids=planning_stop_ids,
    )

    assert stats.routes == 2
    assert stats.trips == 2
    assert stats.stops == 4

    with zipfile.ZipFile(output) as archive:
        assert "levels.txt" not in archive.namelist()
        assert _read_rows(archive, "agency.txt") == [
            {"agency_id": "YUN_34", "agency_name": "Yunlin"},
            {"agency_id": "THB_21", "agency_name": "Intercity"},
        ]
        assert _read_rows(archive, "routes.txt") == [
            {
                "route_id": "YUN_ROUTE",
                "agency_id": "YUN_34",
                "route_short_name": "201",
                "route_type": "3",
            },
            {
                "route_id": "THB_ROUTE",
                "agency_id": "THB_21",
                "route_short_name": "7120",
                "route_type": "3",
            },
        ]
        assert _read_rows(archive, "trips.txt") == [
            {
                "route_id": "YUN_ROUTE",
                "service_id": "YUN_SERVICE",
                "trip_id": "YUN_TRIP",
                "shape_id": "YUN_SHAPE",
            },
            {
                "route_id": "THB_ROUTE",
                "service_id": "THB_SERVICE",
                "trip_id": "THB_TRIP",
                "shape_id": "THB_SHAPE",
            },
        ]
        assert _read_rows(archive, "stops.txt") == [
            {
                "stop_id": "YUN_STOP",
                "stop_name": "Yunlin Stop",
                "stop_lat": "23.69",
                "stop_lon": "120.53",
            },
            {
                "stop_id": "YUN_STOP_2",
                "stop_name": "Yunlin Stop 2",
                "stop_lat": "23.70",
                "stop_lon": "120.54",
            },
            {
                "stop_id": "THB_STOP",
                "stop_name": "Intercity Stop",
                "stop_lat": "23.70",
                "stop_lon": "120.54",
            },
            {
                "stop_id": "THB_STOP_2",
                "stop_name": "Intercity Stop 2",
                "stop_lat": "23.71",
                "stop_lon": "120.55",
            },
        ]

    assert write_stop_index(output, index, planning_stop_ids) == 3
    assert json.loads(index.read_text(encoding="utf-8")) == {
        "schema_version": 1,
        "stops": [
            {
                "id": "THB_STOP",
                "name": "Intercity Stop",
                "latitude": 23.7,
                "longitude": 120.54,
            },
            {
                "id": "YUN_STOP",
                "name": "Yunlin Stop",
                "latitude": 23.69,
                "longitude": 120.53,
            },
            {
                "id": "YUN_STOP_2",
                "name": "Yunlin Stop 2",
                "latitude": 23.7,
                "longitude": 120.54,
            },
        ],
    }
