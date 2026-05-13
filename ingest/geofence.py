"""Geofence loader + point-in-polygon classifier for OSM port polygons.

Adapted from the original golden-path geofence module. Differences:
  - Loads polygons from per-port GeoJSON files in data/port_geofences/
    (one file per UN/LOCODE), produced by ingest.osm_ports.
  - Uses shapely for point-in-polygon (faster + handles MultiPolygon).
  - No berth/anchorage split — OSM polygons describe the whole port.
    `find_port_zone` returns ("LOCODE", "anchorage") for any hit; the
    anchored-vs-berthed distinction is made downstream from speed.
  - Chokepoint loader removed.

Public API:
  - load_ports()            → dict[locode, port_info]
  - find_port_zone(lat,lon) → (locode, "anchorage") | None
  - get_port(locode)        → port_info | None
"""
from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path

from shapely.geometry import Point, shape
from shapely.geometry.base import BaseGeometry

logger = logging.getLogger(__name__)

_THIS_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _THIS_DIR.parent
_PORT_DATA_DIR = _PROJECT_ROOT / "data" / "port_geofences"


def point_in_polygon(lat: float, lon: float, geom: BaseGeometry) -> bool:
    """Return True if (lat, lon) lies within `geom`. geom is a shapely shape."""
    return geom.covers(Point(lon, lat))


@lru_cache(maxsize=1)
def load_ports(path: str | None = None) -> dict:
    """Load port geofences from per-locode GeoJSON files.

    Returns a dict keyed by UN/LOCODE::

        {
            "SGSIN": {
                "name": "Singapore",
                "source": "osm" | "manual_bbox",
                "polygon": <shapely geometry>,
                "centroid": (lat, lon),
            },
            ...
        }
    """
    data_dir = Path(path) if path else _PORT_DATA_DIR
    ports: dict = {}
    if not data_dir.exists():
        logger.warning("Port geofence dir missing: %s", data_dir)
        return ports

    for geojson_path in sorted(data_dir.glob("*.geojson")):
        try:
            feature = json.loads(geojson_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.error("Failed to parse %s: %s", geojson_path, exc)
            continue

        props = feature.get("properties", {})
        geom_json = feature.get("geometry")
        if not geom_json:
            continue
        try:
            geom = shape(geom_json)
        except Exception as exc:
            logger.error("Bad geometry in %s: %s", geojson_path, exc)
            continue

        locode = props.get("locode") or geojson_path.stem
        ports[locode] = {
            "name": props.get("name", locode),
            "source": props.get("source", "osm"),
            "polygon": geom,
            "centroid": (float(geom.centroid.y), float(geom.centroid.x)),
        }

    logger.info("Loaded %d port geofences", len(ports))
    return ports


def find_port_zone(lat: float, lon: float,
                   ports: dict | None = None) -> tuple[str, str] | None:
    """Classify a position into a port zone.

    Returns (locode, "anchorage") for the first port whose polygon contains
    the point, or None. The "berth" vs "anchored" distinction is made by
    the congestion tracker from speed, not from sub-zone geometry.
    """
    _ports = ports if ports is not None else load_ports()
    pt = Point(lon, lat)
    for code, port in _ports.items():
        if port["polygon"].covers(pt):
            return (code, "anchorage")
    return None


def get_port(locode: str, ports: dict | None = None) -> dict | None:
    _ports = ports if ports is not None else load_ports()
    return _ports.get(locode)
