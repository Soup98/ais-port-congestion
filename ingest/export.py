"""Snapshot exporter — reads SQLite and writes versioned JSON files to
``snapshots/`` for the frontend replay loop.

Outputs (each wrapped as {"version": 1, "generated_at": ISO8601, "data": [...]}):
  - snapshots/ports.json                — per-port summary + polygon
  - snapshots/vessel_positions_sample.json — last 24h, capped at 5000 rows
  - snapshots/port_visits.json          — visits with anchorage_exit in last 7d

CLI:
    python -m ingest.export
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from statistics import mean, median

from sqlalchemy import select, func

from db.engine import SessionLocal, engine, Base
from db import models  # noqa: F401
from db.models import (
    VesselPosition, PortVisit, CongestionSnapshot, PortGeofence,
)


SNAPSHOT_DIR = Path(__file__).resolve().parent.parent / "snapshots"
VESSEL_SAMPLE_CAP = 5000
VESSEL_SAMPLE_HOURS = 24
VISITS_WINDOW_DAYS = 7


def _envelope(data) -> dict:
    return {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "data": data,
    }


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.isoformat(timespec="seconds")


CONGESTION_DELTA_PCT = 50.0


def export_ports(session) -> list[dict]:
    rows = session.execute(select(PortGeofence)).scalars().all()

    # Per-port mean wait over the whole window of available visits.
    per_port_visits: dict[str, list[float]] = {}
    for port in rows:
        waits = session.execute(
            select(PortVisit.wait_time_hours)
            .where(PortVisit.locode == port.locode)
        ).scalars().all()
        per_port_visits[port.locode] = [w for w in waits if w is not None]

    per_port_mean: dict[str, float] = {
        loc: mean(ws) for loc, ws in per_port_visits.items() if ws
    }
    # Cross-port baseline = median of per-port means (one demo network can't
    # provide a historical baseline, so use peer ports as the reference).
    baseline = median(per_port_mean.values()) if per_port_mean else 0.0

    out: list[dict] = []
    for port in rows:
        port_mean = per_port_mean.get(port.locode)
        delta_pct = (
            ((port_mean - baseline) / baseline) * 100.0
            if port_mean is not None and baseline > 0 else None
        )
        out.append({
            "locode": port.locode,
            "name": port.name,
            "source": port.source,
            "centroid": {"lat": port.centroid_lat, "lng": port.centroid_lng},
            "polygon": json.loads(port.polygon_geojson),
            "current_mean_wait_hours": round(port_mean, 2) if port_mean is not None else None,
            "baseline_wait_hours": round(baseline, 2),
            "delta_pct": round(delta_pct, 2) if delta_pct is not None else None,
            "vessels_anchored": len(per_port_visits.get(port.locode, [])),
            "is_congested": bool(delta_pct is not None and delta_pct >= CONGESTION_DELTA_PCT),
        })
    return out


def export_vessel_sample(session) -> list[dict]:
    max_ts = session.execute(select(func.max(VesselPosition.timestamp))).scalar()
    if max_ts is None:
        return []
    cutoff = max_ts - timedelta(hours=VESSEL_SAMPLE_HOURS)
    rows = session.execute(
        select(
            VesselPosition.mmsi, VesselPosition.vessel_name,
            VesselPosition.vessel_type, VesselPosition.lat, VesselPosition.lng,
            VesselPosition.speed_knots, VesselPosition.course,
            VesselPosition.timestamp,
        )
        .where(VesselPosition.timestamp >= cutoff)
        .order_by(VesselPosition.timestamp.asc())
        .limit(VESSEL_SAMPLE_CAP)
    ).all()

    return [
        {
            "mmsi": r.mmsi,
            "name": r.vessel_name,
            "type": r.vessel_type,
            "lat": r.lat,
            "lng": r.lng,
            "speed_knots": r.speed_knots,
            "course": r.course,
            "timestamp": _iso(r.timestamp),
        }
        for r in rows
    ]


def export_port_visits(session) -> list[dict]:
    max_dep = session.execute(select(func.max(PortVisit.departure))).scalar()
    if max_dep is None:
        return []
    cutoff = max_dep - timedelta(days=VISITS_WINDOW_DAYS)
    rows = session.execute(
        select(PortVisit)
        .where(PortVisit.departure >= cutoff)
        .order_by(PortVisit.departure.desc())
    ).scalars().all()

    return [
        {
            "mmsi": v.mmsi,
            "locode": v.locode,
            "anchorage_entry": _iso(v.anchorage_entry),
            "berth_entry": _iso(v.berth_entry),
            "departure": _iso(v.departure),
            "wait_time_hours": v.wait_time_hours,
            "status": v.status,
        }
        for v in rows
    ]


def write_json(path: Path, payload: dict) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, ensure_ascii=False)
    path.write_text(text, encoding="utf-8")
    return len(text.encode("utf-8"))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.parse_args()

    Base.metadata.create_all(engine)
    session = SessionLocal()
    try:
        ports = export_ports(session)
        sample = export_vessel_sample(session)
        visits = export_port_visits(session)

        size_p = write_json(SNAPSHOT_DIR / "ports.json", _envelope(ports))
        size_v = write_json(SNAPSHOT_DIR / "vessel_positions_sample.json", _envelope(sample))
        size_x = write_json(SNAPSHOT_DIR / "port_visits.json", _envelope(visits))

        total_mb = (size_p + size_v + size_x) / (1024 * 1024)
        print(f"ports={len(ports)} ({size_p:,}B)")
        print(f"vessel_sample={len(sample)} ({size_v:,}B)")
        print(f"port_visits={len(visits)} ({size_x:,}B)")
        print(f"total={total_mb:.2f} MB")
        if total_mb > 10:
            print("WARNING: snapshot exceeds 10 MB cap")
    finally:
        session.close()


if __name__ == "__main__":
    main()
