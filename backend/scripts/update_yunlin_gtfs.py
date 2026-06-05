"""Download TDX GTFS and keep only Yunlin bus data for OTP."""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
from dotenv import dotenv_values

_TOKEN_URL = "https://tdx.transportdata.tw/auth/realms/TDXConnect/protocol/openid-connect/token"
_TDX_GTFS_URL = "https://tdx.transportdata.tw/api/gtfs/V3/Map/GTFS/Static"
_BACKEND_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_OUTPUT = _BACKEND_ROOT / "otp/data/yunlin-gtfs.zip"
_DEFAULT_STOP_INDEX_OUTPUT = _BACKEND_ROOT / "otp/data/yunlin-stop-index.json"
_YUNLIN_AGENCY_PREFIX = "YUN_"
_YUNLIN_CITY = "YunlinCounty"
_YUNLIN_CITY_CODE = "YUN"
_GTFS_BUS_ROUTE_TYPE = "3"
_TDX_CITY_STOP_URL = f"https://tdx.transportdata.tw/api/basic/v2/Bus/Stop/City/{_YUNLIN_CITY}"
_TDX_INTERCITY_STOP_URL = "https://tdx.transportdata.tw/api/basic/v2/Bus/Stop/InterCity"
_REQUIRED_FILES = {
    "agency.txt",
    "routes.txt",
    "trips.txt",
    "stop_times.txt",
    "stops.txt",
    "calendar.txt",
}


@dataclass(frozen=True)
class Table:
    fields: list[str]
    rows: list[dict[str, str]]


@dataclass(frozen=True)
class FilterStats:
    agencies: int
    routes: int
    trips: int
    stops: int


def _read_table(archive: zipfile.ZipFile, name: str) -> Table | None:
    try:
        payload = archive.read(name)
    except KeyError:
        if name in _REQUIRED_FILES:
            raise RuntimeError(f"TDX GTFS is missing required file {name}") from None
        return None

    reader = csv.DictReader(io.StringIO(payload.decode("utf-8-sig")))
    if reader.fieldnames is None:
        raise RuntimeError(f"TDX GTFS file {name} has no CSV header")
    return Table(fields=list(reader.fieldnames), rows=list(reader))


def _write_table(archive: zipfile.ZipFile, name: str, table: Table) -> None:
    output = io.StringIO()
    writer = csv.DictWriter(
        output,
        fieldnames=table.fields,
        extrasaction="ignore",
        lineterminator="\n",
    )
    writer.writeheader()
    writer.writerows(table.rows)
    archive.writestr(name, output.getvalue().encode())


def _dedupe_rows(table: Table, key: str) -> Table:
    seen: set[str] = set()
    rows: list[dict[str, str]] = []
    for row in table.rows:
        value = row.get(key, "")
        if value and value not in seen:
            seen.add(value)
            rows.append(row)
    return Table(fields=table.fields, rows=rows)


def _drop_field(table: Table, field: str) -> Table:
    if field not in table.fields:
        return table
    return Table(
        fields=[name for name in table.fields if name != field],
        rows=table.rows,
    )


def _rows_with(table: Table, key: str, allowed: set[str]) -> Table:
    return Table(
        fields=table.fields,
        rows=[row for row in table.rows if row.get(key, "") in allowed],
    )


def _ids(table: Table, key: str) -> set[str]:
    return {row[key] for row in table.rows if row.get(key)}


def filter_yunlin_gtfs(
    source: Path,
    output: Path,
    planning_stop_ids: set[str],
) -> FilterStats:
    """Filter a TDX GTFS bundle into a Yunlin planning feed for OTP."""
    with zipfile.ZipFile(source) as source_zip:
        agencies = _read_table(source_zip, "agency.txt")
        routes = _read_table(source_zip, "routes.txt")
        trips = _read_table(source_zip, "trips.txt")
        stop_times = _read_table(source_zip, "stop_times.txt")
        stops = _read_table(source_zip, "stops.txt")
        calendar = _read_table(source_zip, "calendar.txt")
        calendar_dates = _read_table(source_zip, "calendar_dates.txt")
        shapes = _read_table(source_zip, "shapes.txt")
        frequencies = _read_table(source_zip, "frequencies.txt")

    assert agencies is not None
    assert routes is not None
    assert trips is not None
    assert stop_times is not None
    assert stops is not None
    assert calendar is not None

    unique_trips = _dedupe_rows(trips, "trip_id")
    yunlin_agency_ids = {row["agency_id"] for row in agencies.rows if row.get("agency_id", "").startswith(_YUNLIN_AGENCY_PREFIX)}
    local_route_ids = {
        row["route_id"] for row in routes.rows if row.get("agency_id", "") in yunlin_agency_ids and row.get("route_type", "") == _GTFS_BUS_ROUTE_TYPE
    }
    planning_trip_ids = _ids(_rows_with(stop_times, "stop_id", planning_stop_ids), "trip_id")
    planning_route_ids = {row["route_id"] for row in unique_trips.rows if row.get("trip_id", "") in planning_trip_ids}
    route_ids = {
        row["route_id"]
        for row in routes.rows
        if row.get("route_id", "") in local_route_ids | planning_route_ids and row.get("route_type", "") == _GTFS_BUS_ROUTE_TYPE
    }

    yunlin_trips = _rows_with(unique_trips, "route_id", route_ids)
    trip_ids = _ids(yunlin_trips, "trip_id")
    yunlin_stop_times = _rows_with(stop_times, "trip_id", trip_ids)
    route_ids = _ids(yunlin_trips, "route_id")
    yunlin_routes = _rows_with(routes, "route_id", route_ids)
    selected_agency_ids = _ids(yunlin_routes, "agency_id")
    yunlin_agencies = _rows_with(agencies, "agency_id", selected_agency_ids)
    service_ids = _ids(yunlin_trips, "service_id")
    shape_ids = _ids(yunlin_trips, "shape_id")

    stop_ids = _ids(yunlin_stop_times, "stop_id")
    yunlin_stops = _rows_with(_drop_field(stops, "level_id"), "stop_id", stop_ids)
    yunlin_calendar = _rows_with(calendar, "service_id", service_ids)

    output_tables: dict[str, Table] = {
        "agency.txt": yunlin_agencies,
        "routes.txt": yunlin_routes,
        "trips.txt": yunlin_trips,
        "stop_times.txt": yunlin_stop_times,
        "stops.txt": yunlin_stops,
        "calendar.txt": yunlin_calendar,
    }
    if calendar_dates is not None:
        output_tables["calendar_dates.txt"] = _rows_with(calendar_dates, "service_id", service_ids)
    if shapes is not None:
        output_tables["shapes.txt"] = _rows_with(shapes, "shape_id", shape_ids)
    if frequencies is not None:
        output_tables["frequencies.txt"] = _rows_with(frequencies, "trip_id", trip_ids)

    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as output_zip:
        for name, table in output_tables.items():
            _write_table(output_zip, name, table)

    return FilterStats(
        agencies=len(yunlin_agencies.rows),
        routes=len(yunlin_routes.rows),
        trips=len(yunlin_trips.rows),
        stops=len(yunlin_stops.rows),
    )


def write_stop_index(
    gtfs: Path,
    output: Path,
    planning_stop_ids: set[str],
) -> int:
    """Write stops eligible as Yunlin plan_route destinations."""
    with zipfile.ZipFile(gtfs) as archive:
        stops = _read_table(archive, "stops.txt")
    assert stops is not None

    index_stops: list[dict[str, str | float]] = []
    for row in stops.rows:
        stop_id = row.get("stop_id", "")
        name = row.get("stop_name", "")
        if stop_id not in planning_stop_ids or not name:
            continue
        try:
            latitude = float(row["stop_lat"])
            longitude = float(row["stop_lon"])
        except (KeyError, TypeError, ValueError):
            continue
        index_stops.append(
            {
                "id": stop_id,
                "name": name,
                "latitude": latitude,
                "longitude": longitude,
            }
        )

    index_stops.sort(key=lambda stop: (str(stop["name"]), str(stop["id"])))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "stops": index_stops,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return len(index_stops)


def _tdx_credentials(env_file: Path) -> tuple[str, str]:
    env_values: dict[str, Any] = {}
    if env_file.exists():
        env_values = dotenv_values(env_file)

    client_id = os.getenv("TDX_CLIENT_ID") or env_values.get("TDX_CLIENT_ID")
    client_secret = os.getenv("TDX_CLIENT_SECRET") or env_values.get("TDX_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise RuntimeError("TDX_CLIENT_ID and TDX_CLIENT_SECRET must be set in env or --env-file")
    return str(client_id), str(client_secret)


def _get_tdx_token(session: requests.Session, client_id: str, client_secret: str) -> str:
    response = session.post(
        _TOKEN_URL,
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        },
        timeout=30,
    )
    response.raise_for_status()
    token = response.json().get("access_token")
    if not token:
        raise RuntimeError("TDX token response has no access_token")
    return str(token)


def _download_tdx_gtfs(session: requests.Session, token: str, output: Path) -> None:
    with session.get(
        _TDX_GTFS_URL,
        headers={"Authorization": f"Bearer {token}"},
        stream=True,
        timeout=(30, 300),
    ) as response:
        response.raise_for_status()
        with output.open("wb") as gtfs_zip:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    gtfs_zip.write(chunk)


def _tdx_stop_uids(
    session: requests.Session,
    token: str,
    url: str,
    *,
    params: dict[str, str],
) -> set[str]:
    response = session.get(
        url,
        headers={"Authorization": f"Bearer {token}"},
        params=params,
        timeout=90,
    )
    response.raise_for_status()
    stops = response.json()
    if not isinstance(stops, list):
        raise RuntimeError("TDX stop catalog response is not a list")

    stop_uids: set[str] = set()
    for stop in stops:
        if not isinstance(stop, dict):
            continue
        stop_uid = stop.get("StopUID")
        city_code = stop.get("LocationCityCode")
        if isinstance(stop_uid, str) and city_code == _YUNLIN_CITY_CODE:
            stop_uids.add(stop_uid)
    return stop_uids


def _fetch_yunlin_stop_ids(session: requests.Session, token: str) -> set[str]:
    city_stop_ids = _tdx_stop_uids(
        session,
        token,
        _TDX_CITY_STOP_URL,
        params={
            "$select": "StopUID,LocationCityCode",
            "$format": "JSON",
        },
    )
    intercity_stop_ids = _tdx_stop_uids(
        session,
        token,
        _TDX_INTERCITY_STOP_URL,
        params={
            "$filter": f"LocationCityCode eq '{_YUNLIN_CITY_CODE}'",
            "$select": "StopUID,LocationCityCode",
            "$format": "JSON",
        },
    )
    stop_ids = city_stop_ids | intercity_stop_ids
    if not stop_ids:
        raise RuntimeError("TDX Yunlin stop catalog is empty")
    return stop_ids


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download TDX GTFS and filter it to Yunlin agencies.")
    parser.add_argument(
        "--env-file",
        type=Path,
        default=_BACKEND_ROOT / ".env",
        help="dotenv file containing TDX_CLIENT_ID and TDX_CLIENT_SECRET",
    )
    parser.add_argument(
        "--input",
        type=Path,
        help="existing full TDX GTFS zip; skips the static bundle download",
    )
    parser.add_argument(
        "--download-output",
        type=Path,
        help="optional path to keep the downloaded full TDX GTFS zip",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=_DEFAULT_OUTPUT,
        help=f"filtered GTFS output path (default: {_DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--stop-index-output",
        type=Path,
        default=_DEFAULT_STOP_INDEX_OUTPUT,
        help=(f"Yunlin stop index for plan_route (default: {_DEFAULT_STOP_INDEX_OUTPUT})"),
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    client_id, client_secret = _tdx_credentials(args.env_file)
    with requests.Session() as session:
        token = _get_tdx_token(session, client_id, client_secret)
        yunlin_stop_ids = _fetch_yunlin_stop_ids(session, token)
        if args.input is not None:
            stats = filter_yunlin_gtfs(
                args.input,
                args.output,
                planning_stop_ids=yunlin_stop_ids,
            )
        else:
            with tempfile.TemporaryDirectory(prefix="tdx-gtfs-") as tmp_dir:
                tdx_gtfs = Path(tmp_dir) / "tdx-gtfs.zip"
                _download_tdx_gtfs(session, token, tdx_gtfs)
                if args.download_output is not None:
                    args.download_output.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(tdx_gtfs, args.download_output)
                stats = filter_yunlin_gtfs(
                    tdx_gtfs,
                    args.output,
                    planning_stop_ids=yunlin_stop_ids,
                )

    stop_index_entries = write_stop_index(
        args.output,
        args.stop_index_output,
        yunlin_stop_ids,
    )
    size_kb = args.output.stat().st_size // 1024
    print(f"Wrote {args.output} ({size_kb} KB, {stats.routes} routes, {stats.trips} trips, {stats.stops} stops, {stats.agencies} agencies)")
    print(f"Wrote {args.stop_index_output} ({stop_index_entries} Yunlin stops)")


if __name__ == "__main__":
    main()
