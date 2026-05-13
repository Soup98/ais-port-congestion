"""Synthetic AIS vessel-position generator for portfolio demo.

Replaces the Railway CSV loader (B5) because the live aisstream subscription
expired. Generates realistic-looking vessel tracks for the 5 target ports
with engineered congestion states so the demo always renders something
interesting.

Per port we generate N vessels. Each vessel:
  1. Approaches the port from outside (a few positions at 8-12 kn).
  2. Enters the polygon and slows to ~0 kn (anchored).
  3. Sits at anchor for a per-port mean wait time + jitter.
  4. Departs the polygon (a few positions at 5-10 kn outbound).

Inserts into the `vessel_positions` table. Idempotent on (mmsi, timestamp).

CLI:
    python -m ingest.generate_synthetic [--days 7]
"""
from __future__ import annotations

import argparse
import math
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from shapely.geometry import Point

from db.engine import SessionLocal, engine, Base
from db import models  # noqa: F401
from db.models import VesselPosition
from .geofence import load_ports


SAMPLE_INTERVAL = timedelta(minutes=15)

# Vessel name pool (generic, no real vessel data)
NAME_POOL = [
    "PACIFIC STAR", "ATLANTIC PIONEER", "NORTH WIND", "SOUTH CROSS",
    "BLUE HORIZON", "RED ARROW", "SILVER WAVE", "GOLDEN ANCHOR",
    "EAST DAWN", "WEST GLORY", "MERIDIAN", "ENDEAVOR",
    "MARINER", "VOYAGER", "TRADEWIND", "OCEAN PRIDE",
    "STELLA MARIS", "CAPE GUARDIAN", "DELTA QUEEN", "POLARIS",
]

VESSEL_TYPES = ["Cargo", "Tanker", "Container", "Bulk Carrier", "Tanker", "Cargo"]


@dataclass
class PortScenario:
    locode: str
    n_vessels: int
    mean_wait_hours: float
    wait_jitter_hours: float
    label: str  # "congested" | "moderate" | "clear"


# Engineered scenarios — different congestion states across ports
SCENARIOS: list[PortScenario] = [
    PortScenario("SGSIN", n_vessels=12, mean_wait_hours=30.0, wait_jitter_hours=10.0, label="congested"),
    PortScenario("CNSHA", n_vessels=10, mean_wait_hours=24.0, wait_jitter_hours=8.0,  label="congested"),
    PortScenario("USLAX", n_vessels=6,  mean_wait_hours=10.0, wait_jitter_hours=4.0,  label="moderate"),
    PortScenario("NLRTM", n_vessels=4,  mean_wait_hours=4.0,  wait_jitter_hours=2.0,  label="clear"),
    PortScenario("DEHAM", n_vessels=4,  mean_wait_hours=5.0,  wait_jitter_hours=2.0,  label="clear"),
]


def _interior_point(geom) -> tuple[float, float]:
    """Return (lat, lon) of a point guaranteed inside the geometry."""
    p = geom.representative_point()
    return (float(p.y), float(p.x))


def _exterior_offset(centroid: tuple[float, float], bearing_deg: float,
                     distance_deg: float) -> tuple[float, float]:
    """Return a (lat, lon) point offset from centroid along bearing."""
    lat, lon = centroid
    rad = math.radians(bearing_deg)
    return (lat + distance_deg * math.cos(rad), lon + distance_deg * math.sin(rad))


def _generate_vessel(rng: random.Random, port_locode: str,
                     port_geom, mean_wait: float, jitter: float,
                     window_start: datetime, window_end: datetime,
                     mmsi: int) -> list[dict]:
    """Generate one vessel's track: approach → anchor → depart."""
    anchor_lat, anchor_lon = _interior_point(port_geom)
    # jitter the anchor point within polygon by a tiny offset
    for _ in range(10):
        dlat = rng.uniform(-0.005, 0.005)
        dlon = rng.uniform(-0.005, 0.005)
        cand = Point(anchor_lon + dlon, anchor_lat + dlat)
        if port_geom.covers(cand):
            anchor_lat += dlat
            anchor_lon += dlon
            break

    wait_hours = max(1.5, rng.gauss(mean_wait, jitter))
    # Approach window: 1-2 h before anchorage entry
    approach_steps = rng.randint(4, 8)
    depart_steps = rng.randint(4, 8)
    anchor_steps = max(2, int(wait_hours * 60 / SAMPLE_INTERVAL.total_seconds() * 60))
    # Fix: anchor_steps = wait_hours / (interval in hours)
    anchor_steps = max(2, int(wait_hours / (SAMPLE_INTERVAL.total_seconds() / 3600)))

    total_steps = approach_steps + anchor_steps + depart_steps
    total_span = SAMPLE_INTERVAL * total_steps
    latest_start = window_end - total_span
    if latest_start <= window_start:
        start = window_start
    else:
        start = window_start + timedelta(
            seconds=rng.uniform(0, (latest_start - window_start).total_seconds())
        )

    approach_bearing = rng.uniform(0, 360)
    depart_bearing = (approach_bearing + 180 + rng.uniform(-40, 40)) % 360

    positions: list[dict] = []
    name = rng.choice(NAME_POOL) + f" {rng.randint(1, 9)}"
    vtype = rng.choice(VESSEL_TYPES)

    # Approach: from ~0.15° away → anchor point
    for i in range(approach_steps):
        frac = (i + 1) / (approach_steps + 1)
        dist = 0.15 * (1 - frac)
        lat, lon = _exterior_offset((anchor_lat, anchor_lon), approach_bearing, dist)
        speed = rng.uniform(8.0, 12.0) * (1 - frac * 0.6)
        ts = start + SAMPLE_INTERVAL * i
        positions.append({
            "mmsi": mmsi, "name": name, "vtype": vtype,
            "lat": lat, "lon": lon, "speed": round(speed, 1),
            "course": approach_bearing, "ts": ts,
        })

    # Anchor: jitter ±0.001° around anchor point, speed near 0
    for i in range(anchor_steps):
        lat = anchor_lat + rng.uniform(-0.001, 0.001)
        lon = anchor_lon + rng.uniform(-0.001, 0.001)
        speed = rng.uniform(0.0, 0.3)
        ts = start + SAMPLE_INTERVAL * (approach_steps + i)
        positions.append({
            "mmsi": mmsi, "name": name, "vtype": vtype,
            "lat": lat, "lon": lon, "speed": round(speed, 2),
            "course": rng.uniform(0, 360), "ts": ts,
        })

    # Depart: anchor → 0.15° away
    for i in range(depart_steps):
        frac = (i + 1) / (depart_steps + 1)
        dist = 0.15 * frac
        lat, lon = _exterior_offset((anchor_lat, anchor_lon), depart_bearing, dist)
        speed = rng.uniform(5.0, 10.0) * (0.4 + frac * 0.6)
        ts = start + SAMPLE_INTERVAL * (approach_steps + anchor_steps + i)
        positions.append({
            "mmsi": mmsi, "name": name, "vtype": vtype,
            "lat": lat, "lon": lon, "speed": round(speed, 1),
            "course": depart_bearing, "ts": ts,
        })

    return positions


def generate(days: int = 7, seed: int = 42) -> int:
    rng = random.Random(seed)
    Base.metadata.create_all(engine)
    ports = load_ports()
    if not ports:
        print("No port geofences loaded. Run ingest.osm_ports first.")
        return 0

    # Floor to top of UTC day so reruns within the same day are idempotent.
    # Use naive UTC because SQLite stores datetimes as naive — keeping both
    # sides naive makes the (mmsi, timestamp) idempotency check work.
    today = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0, tzinfo=None,
    )
    window_end = today
    window_start = today - timedelta(days=days)

    mmsi_counter = 200000000  # synthetic range
    all_rows: list[dict] = []

    for scenario in SCENARIOS:
        port = ports.get(scenario.locode)
        if port is None:
            print(f"[{scenario.locode}] skipped — no geofence")
            continue
        geom = port["polygon"]
        for _ in range(scenario.n_vessels):
            mmsi_counter += 1
            track = _generate_vessel(
                rng, scenario.locode, geom,
                scenario.mean_wait_hours, scenario.wait_jitter_hours,
                window_start, window_end, mmsi_counter,
            )
            all_rows.extend(track)
        print(f"[{scenario.locode}] {scenario.n_vessels} vessels generated ({scenario.label})")

    session = SessionLocal()
    inserted = 0
    skipped = 0
    try:
        existing = {
            (m, ts) for (m, ts) in session.query(
                VesselPosition.mmsi, VesselPosition.timestamp
            ).all()
        }
        for row in all_rows:
            key = (str(row["mmsi"]), row["ts"])
            if key in existing:
                skipped += 1
                continue
            session.add(VesselPosition(
                mmsi=str(row["mmsi"]),
                imo=None,
                vessel_name=row["name"],
                vessel_type=row["vtype"],
                lat=row["lat"],
                lng=row["lon"],
                speed_knots=row["speed"],
                course=row["course"],
                timestamp=row["ts"],
            ))
            existing.add(key)
            inserted += 1
        session.commit()
    finally:
        session.close()

    print(f"inserted={inserted} skipped={skipped} total_rows_generated={len(all_rows)}")
    return inserted


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7, help="Time window (days back from now)")
    ap.add_argument("--seed", type=int, default=42, help="RNG seed")
    args = ap.parse_args()
    generate(days=args.days, seed=args.seed)


if __name__ == "__main__":
    main()
