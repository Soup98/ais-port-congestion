"""Smoke test for port visit detection.

A synthetic vessel sits inside a fake port polygon at 0 knots for 5 hours,
then leaves. The tracker should record one PortWaitRecord with
wait_hours close to 5.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from ingest.port_congestion import PortCongestionTracker


def test_vessel_anchored_then_departs_records_visit(monkeypatch):
    # Square polygon centred on (0, 0), spanning roughly +/- 0.5 deg.
    from shapely.geometry import box
    fake_ports = {
        "TESTP": {
            "name": "Test Port",
            "source": "manual_bbox",
            "polygon": box(-0.5, -0.5, 0.5, 0.5),
            "centroid": (0.0, 0.0),
        },
    }

    # Force load_ports() inside the tracker to return our fake set.
    import ingest.geofence as gf
    import ingest.port_congestion as pc
    monkeypatch.setattr(gf, "load_ports", lambda *a, **kw: fake_ports)
    monkeypatch.setattr(pc, "load_ports", lambda *a, **kw: fake_ports)

    tracker = PortCongestionTracker()

    t0 = datetime(2026, 1, 1, 0, 0, 0)
    # Anchored for 5 hours: 21 samples every 15 minutes, all at 0 kn inside polygon
    for i in range(21):
        tracker.process_position({
            "mmsi": 100,
            "ship_name": "TESTER",
            "latitude": 0.0,
            "longitude": 0.0,
            "speed_knots": 0.0,
            "timestamp": t0 + timedelta(minutes=15 * i),
        })

    # Vessel leaves the polygon (lat/lon far outside)
    tracker.process_position({
        "mmsi": 100,
        "ship_name": "TESTER",
        "latitude": 10.0,
        "longitude": 10.0,
        "speed_knots": 8.0,
        "timestamp": t0 + timedelta(hours=5),
    })

    history = tracker.wait_history.get("TESTP", [])
    assert len(history) == 1, f"expected 1 visit, got {len(history)}"
    visit = history[0]
    assert visit.exited_to == "departed"
    # Anchorage entry was t0 (first position inside), departure at t0+5h
    assert abs(visit.wait_hours - 5.0) < 0.01, f"wait_hours={visit.wait_hours}"
