# OTP data

This directory keeps the local OpenTripPlanner build inputs for frontend-driven
route planning. Generated GTFS, OSM extracts and OTP graph files stay out of
git.

## Fetch Yunlin GTFS

TDX serves a Taiwan GTFS bundle. Use the project script to download it with
TDX client credentials and emit the Yunlin OTP GTFS zip plus the generated
Yunlin stop index:

```bash
cd backend
uv run python scripts/update_yunlin_gtfs.py --env-file <path-to-env>
```

The script reads `TDX_CLIENT_ID` and `TDX_CLIENT_SECRET` from the process
environment first, then from `--env-file`. The default outputs are:

```text
otp/data/yunlin-gtfs.zip
otp/data/yunlin-stop-index.json
```

Use `--download-output <path>` only when debugging the full TDX bundle. Keep
that large source zip outside git.

To filter an already downloaded TDX GTFS zip instead of downloading the full
static bundle again:

```bash
cd backend
uv run python scripts/update_yunlin_gtfs.py \
  --input <path-to-tdx-gtfs.zip> \
  --output otp/data/yunlin-gtfs.zip
```

`--input` still calls TDX stop metadata endpoints with the same credentials so
the county stop index stays current.

The filter uses TDX stop metadata as the county boundary for planning. It keeps:

- bus routes owned by `YUN_` agencies
- bus routes with at least one GTFS stop whose TDX `LocationCityCode` is `YUN`

It trims dependent GTFS tables to the trips, stops, services and shapes that
those routes reference. It keeps selected trips' full stop sequences: TDX has
trips whose intermediate stop times are interpolated by OTP, and clipping those
rows to the OSM bounds can remove the final time needed for graph build. It also
removes `stops.txt` `level_id` references and deduplicates `trip_id` rows before
OTP sees the feed. The generated `yunlin-stop-index.json` keeps only canonical
Yunlin stop UIDs present in that graph feed; the Kiosk planner resolves the
configured origin stop against that index while frontend map selection provides
the destination coordinate.

`7120` and `7126` come from TDX THB agencies and are included because TDX marks
their Yunlin stops with `LocationCityCode=YUN`. The current TDX static GTFS has
`7000D`, not the ebus Kiosk variant
`7000B`; treat that route name mismatch as a product/data validation item.

## Build and run OTP

The first OSM input is the Yunlin extract in:

```text
otp/data/yunlin.osm.pbf
```

This file is not committed and has no fetch script; it was previously
generated ad hoc and the source was never recorded. To regenerate it:

```bash
# 1. Download the Taiwan-wide extract (~300 MB)
curl -L -o /tmp/taiwan-latest.osm.pbf https://download.geofabrik.de/asia/taiwan-latest.osm.pbf

# 2. Clip to Yunlin County's official OSM boundary (relation 2915930) plus a
#    ~0.1° buffer so routes that cross the county line (e.g. THB intercity
#    routes 7120/7126) don't get cut off mid-road. Bounding box source:
#    https://nominatim.openstreetmap.org/search?q=Yunlin+County,+Taiwan&format=json
brew install osmium-tool
osmium extract -b 119.88,23.31,120.84,23.97 \
  /tmp/taiwan-latest.osm.pbf -o otp/data/yunlin.osm.pbf
```

Build the OTP graph after GTFS and OSM inputs are present:

```bash
cd backend
docker run --rm \
  -e JAVA_TOOL_OPTIONS="-Xmx4g" \
  -v "$(pwd)/otp/data:/var/opentripplanner" \
  docker.io/opentripplanner/opentripplanner:2.9.0 \
  --build --save
```

Start the local OTP service:

```bash
cd backend
docker compose -f otp/docker-compose.yml up -d
```

The GTFS GraphQL endpoint is served by OTP on:

```text
http://localhost:8081/otp/gtfs/v1
```

GraphQL route planning was verified with `planConnection` from the NYUST stop
area to map-selected Yunlin coordinates after building this graph. Keep
route-planning queries on BUS mode for the first coordinate planner iteration.
The frontend does not call OTP directly; it calls `POST /api/route-plans`, and
the Python API returns MapCN-ready route geometry.
