"""Pull port polygons from OSM Overpass API for target UN/LOCODEs.

Writes GeoJSON to data/port_geofences/{locode}.geojson and upserts a row
into port_geofences. Falls back to manual bbox rectangle when Overpass
returns no usable polygon.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

import requests
from shapely.geometry import Polygon, MultiPolygon, box, mapping, shape
from shapely.ops import unary_union

from db.engine import SessionLocal, engine, Base
from db import models  # noqa: F401
from db.models import PortGeofence


OVERPASS_URL = os.environ.get("OVERPASS_URL", "https://overpass-api.de/api/interpreter")
GEOFENCE_DIR = Path(__file__).resolve().parent.parent / "data" / "port_geofences"


@dataclass
class PortSpec:
    locode: str
    name: str
    # bbox: (south, west, north, east)
    bbox: tuple[float, float, float, float]


TARGETS: list[PortSpec] = [
    PortSpec("SGSIN", "Singapore",   (1.15, 103.55, 1.45, 104.10)),
    PortSpec("NLRTM", "Rotterdam",   (51.85, 3.95, 52.05, 4.55)),
    PortSpec("USLAX", "Los Angeles", (33.68, -118.32, 33.80, -118.14)),
    PortSpec("DEHAM", "Hamburg",     (53.45, 9.85, 53.60, 10.10)),
    PortSpec("CNSHA", "Shanghai",    (30.55, 121.40, 31.55, 122.20)),
]


OVERPASS_QUERY = """
[out:json][timeout:90];
(
  way["harbour"]({s},{w},{n},{e});
  relation["harbour"]({s},{w},{n},{e});
  way["landuse"="harbour"]({s},{w},{n},{e});
  relation["landuse"="harbour"]({s},{w},{n},{e});
  way["industrial"="port"]({s},{w},{n},{e});
  relation["industrial"="port"]({s},{w},{n},{e});
);
out geom;
"""


def query_overpass(spec: PortSpec) -> dict:
    s, w, n, e = spec.bbox
    q = OVERPASS_QUERY.format(s=s, w=w, n=n, e=e)
    headers = {"User-Agent": "ais-port-congestion/0.1 (portfolio project)"}
    resp = requests.post(OVERPASS_URL, data={"data": q}, headers=headers, timeout=120)
    resp.raise_for_status()
    return resp.json()


def _way_polygon(elem: dict) -> Polygon | None:
    geom = elem.get("geometry") or []
    if len(geom) < 4:
        return None
    coords = [(pt["lon"], pt["lat"]) for pt in geom]
    if coords[0] != coords[-1]:
        return None
    try:
        poly = Polygon(coords)
        if not poly.is_valid:
            poly = poly.buffer(0)
        if poly.is_empty or poly.area == 0:
            return None
        return poly
    except Exception:
        return None


def _relation_polygons(elem: dict) -> list[Polygon]:
    polys: list[Polygon] = []
    for member in elem.get("members", []):
        if member.get("type") != "way":
            continue
        if member.get("role") not in ("outer", "", None):
            continue
        geom = member.get("geometry") or []
        if len(geom) < 4:
            continue
        coords = [(pt["lon"], pt["lat"]) for pt in geom]
        if coords[0] != coords[-1]:
            continue
        try:
            p = Polygon(coords)
            if not p.is_valid:
                p = p.buffer(0)
            if not p.is_empty and p.area > 0:
                polys.append(p)
        except Exception:
            continue
    return polys


def polygons_from_overpass(payload: dict) -> list[Polygon]:
    polys: list[Polygon] = []
    for elem in payload.get("elements", []):
        t = elem.get("type")
        if t == "way":
            p = _way_polygon(elem)
            if p is not None:
                polys.append(p)
        elif t == "relation":
            polys.extend(_relation_polygons(elem))
    return polys


def build_geometry(spec: PortSpec) -> tuple[Polygon | MultiPolygon, str]:
    try:
        payload = query_overpass(spec)
        polys = polygons_from_overpass(payload)
    except Exception as exc:
        print(f"[{spec.locode}] Overpass error: {exc}. Using manual bbox.")
        polys = []

    if polys:
        merged = unary_union(polys)
        if isinstance(merged, (Polygon, MultiPolygon)) and not merged.is_empty:
            return merged, "osm"

    s, w, n, e = spec.bbox
    return box(w, s, e, n), "manual_bbox"


def upsert_port(session, spec: PortSpec, geom, source: str) -> None:
    geojson = mapping(geom)
    centroid = geom.centroid
    row = session.get(PortGeofence, spec.locode)
    if row is None:
        row = PortGeofence(locode=spec.locode)
        session.add(row)
    row.name = spec.name
    row.polygon_geojson = json.dumps(geojson)
    row.centroid_lat = float(centroid.y)
    row.centroid_lng = float(centroid.x)
    row.source = source


def write_geojson_file(spec: PortSpec, geom, source: str) -> Path:
    GEOFENCE_DIR.mkdir(parents=True, exist_ok=True)
    out_path = GEOFENCE_DIR / f"{spec.locode}.geojson"
    feature = {
        "type": "Feature",
        "properties": {
            "locode": spec.locode,
            "name": spec.name,
            "source": source,
        },
        "geometry": mapping(geom),
    }
    out_path.write_text(json.dumps(feature, indent=2))
    return out_path


def main() -> None:
    Base.metadata.create_all(engine)
    session = SessionLocal()
    try:
        for spec in TARGETS:
            print(f"[{spec.locode}] querying Overpass...")
            geom, source = build_geometry(spec)
            path = write_geojson_file(spec, geom, source)
            upsert_port(session, spec, geom, source)
            print(f"[{spec.locode}] source={source} area_deg2={geom.area:.4f} -> {path}")
            time.sleep(1.0)  # gentle on public Overpass
        session.commit()
    finally:
        session.close()


if __name__ == "__main__":
    main()
