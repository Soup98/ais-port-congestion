"""Orchestrator: replay vessel_positions through the congestion engine,
persist port_visits, then aggregate per-port-per-day congestion snapshots.

Idempotent: truncates port_visits + congestion_snapshots before rewriting.

CLI:
    python -m ingest.run
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import date, datetime, timedelta
from statistics import mean

from sqlalchemy import select

from db.engine import SessionLocal, engine, Base
from db import models  # noqa: F401
from db.models import (
    VesselPosition, PortVisit, CongestionSnapshot, PortGeofence,
)
from .port_congestion import PortCongestionTracker, PortWaitRecord


BASELINE_WINDOW_DAYS = 30


def _replay(session) -> PortCongestionTracker:
    tracker = PortCongestionTracker()
    positions = session.execute(
        select(
            VesselPosition.mmsi,
            VesselPosition.vessel_name,
            VesselPosition.lat,
            VesselPosition.lng,
            VesselPosition.speed_knots,
            VesselPosition.timestamp,
        ).order_by(VesselPosition.timestamp.asc())
    ).all()

    for mmsi, name, lat, lng, speed, ts in positions:
        tracker.process_position({
            "mmsi": int(mmsi),
            "ship_name": name or "",
            "latitude": float(lat),
            "longitude": float(lng),
            "speed_knots": float(speed) if speed is not None else 0.0,
            "timestamp": ts,
        })

    # Flush any vessels still tracked at end of feed by forcing eviction.
    # We treat their last_seen as a departure to capture in-progress waits.
    for mmsi in list(tracker.active_vessels.keys()):
        state = tracker.active_vessels[mmsi]
        tracker._record_departure(state, state.last_seen)  # noqa: SLF001

    return tracker


def _write_port_visits(session, tracker: PortCongestionTracker) -> int:
    session.query(PortVisit).delete()
    n = 0
    for locode, records in tracker.wait_history.items():
        for r in records:
            session.add(PortVisit(
                mmsi=str(r.mmsi),
                locode=locode,
                anchorage_entry=r.anchorage_entry,
                berth_entry=r.anchorage_exit if r.exited_to == "berth" else None,
                departure=r.anchorage_exit,
                wait_time_hours=r.wait_hours,
                status=r.exited_to,  # "berth" | "departed"
            ))
            n += 1
    return n


def _aggregate_snapshots(session, tracker: PortCongestionTracker) -> int:
    """Per-port, per-day mean wait + rolling baseline."""
    session.query(CongestionSnapshot).delete()

    by_port: dict[str, list[PortWaitRecord]] = tracker.wait_history
    n = 0
    for locode, records in by_port.items():
        if not records:
            continue
        records = sorted(records, key=lambda r: r.anchorage_exit)

        by_day: dict[date, list[PortWaitRecord]] = defaultdict(list)
        for r in records:
            by_day[r.anchorage_exit.date()].append(r)

        for day, day_records in sorted(by_day.items()):
            day_dt = datetime.combine(day, datetime.min.time())
            baseline_cutoff = day_dt - timedelta(days=BASELINE_WINDOW_DAYS)
            baseline_records = [
                r.wait_hours for r in records
                if baseline_cutoff <= r.anchorage_exit < day_dt
            ]
            day_waits = [r.wait_hours for r in day_records]
            mean_wait = mean(day_waits)
            baseline = mean(baseline_records) if baseline_records else mean_wait
            delta_pct = (
                ((mean_wait - baseline) / baseline) * 100.0
                if baseline > 0 else 0.0
            )

            session.add(CongestionSnapshot(
                locode=locode,
                snapshot_date=day_dt,
                vessels_anchored=len(day_records),
                mean_wait_hours=round(mean_wait, 2),
                baseline_wait_hours=round(baseline, 2),
                delta_pct=round(delta_pct, 2),
            ))
            n += 1
    return n


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.parse_args()

    Base.metadata.create_all(engine)
    session = SessionLocal()
    try:
        port_count = session.query(PortGeofence).count()
        pos_count = session.query(VesselPosition).count()
        print(f"ports={port_count} positions={pos_count}")
        if port_count == 0 or pos_count == 0:
            print("Missing geofences or positions. Run osm_ports + generate_synthetic first.")
            return

        tracker = _replay(session)
        visits = _write_port_visits(session, tracker)
        snaps = _aggregate_snapshots(session, tracker)
        session.commit()
        print(f"port_visits={visits} congestion_snapshots={snaps}")
    finally:
        session.close()


if __name__ == "__main__":
    main()
