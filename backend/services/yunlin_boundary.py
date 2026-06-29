"""Yunlin County service-area boundary checks."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

type LngLat = tuple[float, float]
type LinearRing = tuple[LngLat, ...]
type PolygonCoordinates = tuple[LinearRing, ...]
type MultiPolygonCoordinates = tuple[PolygonCoordinates, ...]

_BOUNDARY_PATH = Path(__file__).resolve().parents[1] / "data/geo/yunlin-county.geojson"
_EPSILON = 1e-10
_MAINLAND_MIN_BBOX_AREA_SQ_DEG = 0.01


def _lng_lat_pair(value: Any) -> LngLat | None:
    if not isinstance(value, list | tuple) or len(value) < 2:
        return None
    try:
        lng = float(value[0])
        lat = float(value[1])
    except (TypeError, ValueError):
        return None
    return lng, lat


def _parse_ring(value: Any) -> LinearRing:
    if not isinstance(value, list):
        return ()
    return tuple(pair for item in value if (pair := _lng_lat_pair(item)) is not None)


def _parse_polygon(value: Any) -> PolygonCoordinates:
    if not isinstance(value, list):
        return ()
    return tuple(ring for item in value if len(ring := _parse_ring(item)) >= 4)


def _ring_bbox_area(ring: LinearRing) -> float:
    min_lng = min(lng for lng, _ in ring)
    max_lng = max(lng for lng, _ in ring)
    min_lat = min(lat for _, lat in ring)
    max_lat = max(lat for _, lat in ring)
    return (max_lng - min_lng) * (max_lat - min_lat)


def _is_mainland_polygon(polygon: PolygonCoordinates) -> bool:
    outer_ring = polygon[0]
    return _ring_bbox_area(outer_ring) >= _MAINLAND_MIN_BBOX_AREA_SQ_DEG


@lru_cache(maxsize=1)
def yunlin_polygons() -> MultiPolygonCoordinates:
    payload = json.loads(_BOUNDARY_PATH.read_text(encoding="utf-8"))
    feature = payload["features"][0]
    geometry = feature["geometry"]
    if geometry["type"] != "MultiPolygon":
        raise ValueError("Yunlin boundary must be a MultiPolygon")
    return tuple(polygon for item in geometry["coordinates"] if len(polygon := _parse_polygon(item)) > 0 and _is_mainland_polygon(polygon))


def _point_on_segment(point: LngLat, start: LngLat, end: LngLat) -> bool:
    x, y = point
    x1, y1 = start
    x2, y2 = end
    cross = (x - x1) * (y2 - y1) - (y - y1) * (x2 - x1)
    if abs(cross) > _EPSILON:
        return False
    return min(x1, x2) - _EPSILON <= x <= max(x1, x2) + _EPSILON and min(y1, y2) - _EPSILON <= y <= max(y1, y2) + _EPSILON


def _point_in_ring(point: LngLat, ring: LinearRing) -> bool:
    x, y = point
    inside = False
    previous = len(ring) - 1
    for current, end in enumerate(ring):
        start = ring[previous]
        if _point_on_segment(point, start, end):
            return True

        x1, y1 = start
        x2, y2 = end
        intersects = (y1 > y) != (y2 > y) and x < (((x2 - x1) * (y - y1)) / (y2 - y1) + x1)
        if intersects:
            inside = not inside
        previous = current
    return inside


def _point_in_polygon(point: LngLat, polygon: PolygonCoordinates) -> bool:
    outer_ring, *holes = polygon
    if not _point_in_ring(point, outer_ring):
        return False
    return not any(_point_in_ring(point, hole) for hole in holes)


def is_in_yunlin_county(latitude: float, longitude: float) -> bool:
    point = (longitude, latitude)
    return any(_point_in_polygon(point, polygon) for polygon in yunlin_polygons())
