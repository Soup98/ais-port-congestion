"""Port congestion inference from AIS positions.

Adapted from the golden-path port_congestion module. Differences:
  - Imports from .geofence (no backend.* paths).
  - GDELT cross-validation removed (out of scope for this repo).
  - DB-restore helper removed (this repo persists via ingest.run).
  - No seed_baseline_wait_hours fallback — baseline is empty until enough
    completed waits accumulate, then becomes the rolling 30-day median.

Methodology:
  1. Vessel inside port polygon with speed < 0.5 kn for > 1 hr = anchored.
  2. Wait time = anchorage entry → berth entry (speed > 0.5 kn inside
     polygon after being anchored) OR departure (exit polygon).
  3. Rolling 30-day median wait time per port = baseline.

Public API:
  - PortCongestionTracker  → stateful tracker
  - process_position(pos)  → module-level convenience
  - get_congestion(locode)
  - get_all_congestion()
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from statistics import mean, median

from .geofence import find_port_zone, load_ports

logger = logging.getLogger(__name__)


ANCHORED_SPEED_KN = 0.5
ANCHORED_MIN_DURATION = timedelta(hours=1)
SLOW_APPROACH_SPEED_KN = 3.0
BASELINE_WINDOW_DAYS = 30
CONGESTION_RATIO = 1.5
STALE_VESSEL_HOURS = 24
MIN_WAIT_HOURS = 0.1


@dataclass
class PortVesselState:
    mmsi: int
    ship_name: str
    port: str
    zone: str                              # "anchorage" or "berth"
    entered_port_at: datetime
    entered_zone_at: datetime
    last_seen: datetime
    last_speed: float
    is_anchored: bool = False
    anchored_since: datetime | None = None
    anchorage_entry: datetime | None = None
    nav_status: int | None = None


@dataclass
class PortWaitRecord:
    mmsi: int
    ship_name: str
    port: str
    anchorage_entry: datetime
    anchorage_exit: datetime
    wait_hours: float
    exited_to: str                          # "berth" | "departed"


@dataclass
class PortCongestionSnapshot:
    port: str
    port_name: str
    timestamp: datetime
    vessels_at_anchorage: int
    vessels_at_berth: int
    vessels_anchored: int
    vessels_approaching: int
    avg_wait_hours: float
    baseline_wait_hours: float
    congestion_ratio: float
    is_congested: bool
    recent_departures_24h: int
    confidence: str
    wait_records: list[PortWaitRecord] = field(default_factory=list)


class PortCongestionTracker:
    """Stateful tracker — feed every AIS position through `process_position`."""

    def __init__(self) -> None:
        self._vessels: dict[int, PortVesselState] = {}
        self._wait_records: dict[str, list[PortWaitRecord]] = defaultdict(list)
        self._ports = load_ports()

    # -----------------------------------------------------------------
    # Ingestion
    # -----------------------------------------------------------------

    def process_position(self, pos: dict) -> PortVesselState | None:
        mmsi = pos.get("mmsi")
        lat = pos.get("latitude")
        lon = pos.get("longitude")
        speed = pos.get("speed_knots")
        ts = pos.get("timestamp")
        name = pos.get("ship_name", "")
        nav_status = pos.get("nav_status")

        if mmsi is None or lat is None or lon is None or ts is None:
            return None
        if speed is None:
            speed = 0.0

        zone_result = find_port_zone(lat, lon, ports=self._ports)

        if zone_result is None:
            prev = self._vessels.pop(mmsi, None)
            if prev is not None:
                self._record_departure(prev, ts)
            return None

        unlocode, zone_name = zone_result
        now = ts

        prev = self._vessels.get(mmsi)

        if prev is None:
            state = PortVesselState(
                mmsi=mmsi,
                ship_name=name,
                port=unlocode,
                zone=zone_name,
                entered_port_at=now,
                entered_zone_at=now,
                last_seen=now,
                last_speed=speed,
                anchorage_entry=now if zone_name == "anchorage" else None,
                nav_status=nav_status,
            )
            self._update_anchored_flag(state, speed, now)
            self._vessels[mmsi] = state
            return state

        if prev.port != unlocode:
            self._record_departure(prev, now)
            state = PortVesselState(
                mmsi=mmsi,
                ship_name=name,
                port=unlocode,
                zone=zone_name,
                entered_port_at=now,
                entered_zone_at=now,
                last_seen=now,
                last_speed=speed,
                anchorage_entry=now if zone_name == "anchorage" else None,
                nav_status=nav_status,
            )
            self._update_anchored_flag(state, speed, now)
            self._vessels[mmsi] = state
            return state

        if prev.zone != zone_name:
            if prev.zone == "anchorage" and zone_name == "berth":
                self._record_anchorage_to_berth(prev, now)
            elif prev.zone == "berth" and zone_name == "anchorage":
                prev.anchorage_entry = now

            prev.zone = zone_name
            prev.entered_zone_at = now
            prev.is_anchored = False
            prev.anchored_since = None

        prev.last_seen = now
        prev.last_speed = speed
        prev.ship_name = name or prev.ship_name
        prev.nav_status = nav_status
        self._update_anchored_flag(prev, speed, now)

        return prev

    # -----------------------------------------------------------------
    # Snapshots
    # -----------------------------------------------------------------

    def get_congestion(self, unlocode: str,
                       now: datetime | None = None) -> PortCongestionSnapshot:
        if now is None:
            now = datetime.now(timezone.utc)
        self._evict_stale(now)

        port_info = self._ports.get(unlocode, {})
        port_name = port_info.get("name", unlocode)

        vessels = [v for v in self._vessels.values() if v.port == unlocode]
        at_anchorage = [v for v in vessels if v.zone == "anchorage"]
        at_berth = [v for v in vessels if v.zone == "berth"]
        anchored = [v for v in at_anchorage if v.is_anchored]
        approaching = [
            v for v in at_anchorage
            if not v.is_anchored and v.last_speed < SLOW_APPROACH_SPEED_KN
        ]

        wait_hours_list = []
        for v in anchored:
            if v.anchored_since:
                wait_hours_list.append((now - v.anchored_since).total_seconds() / 3600)
        avg_wait = mean(wait_hours_list) if wait_hours_list else 0.0

        baseline = self._compute_baseline(unlocode, now)
        ratio = avg_wait / baseline if baseline > 0 else 0.0

        cutoff_24h = now - timedelta(hours=24)
        recent = [
            r for r in self._wait_records.get(unlocode, [])
            if r.anchorage_exit >= cutoff_24h
        ]

        total_records = len(self._wait_records.get(unlocode, []))
        if total_records >= 100:
            confidence = "high"
        elif total_records >= 20:
            confidence = "medium"
        else:
            confidence = "low"

        return PortCongestionSnapshot(
            port=unlocode,
            port_name=port_name,
            timestamp=now,
            vessels_at_anchorage=len(at_anchorage),
            vessels_at_berth=len(at_berth),
            vessels_anchored=len(anchored),
            vessels_approaching=len(approaching),
            avg_wait_hours=round(avg_wait, 2),
            baseline_wait_hours=round(baseline, 2),
            congestion_ratio=round(ratio, 2),
            is_congested=ratio >= CONGESTION_RATIO and baseline > 0,
            recent_departures_24h=len(recent),
            confidence=confidence,
            wait_records=list(self._wait_records.get(unlocode, [])),
        )

    def get_all_congestion(self) -> dict[str, PortCongestionSnapshot]:
        active_ports = {v.port for v in self._vessels.values()}
        return {p: self.get_congestion(p) for p in active_ports}

    @property
    def wait_history(self) -> dict[str, list[PortWaitRecord]]:
        return dict(self._wait_records)

    @property
    def active_vessels(self) -> dict[int, PortVesselState]:
        return dict(self._vessels)

    # -----------------------------------------------------------------
    # Internals
    # -----------------------------------------------------------------

    def _update_anchored_flag(self, state: PortVesselState, speed: float,
                              now: datetime) -> None:
        if state.zone != "anchorage":
            state.is_anchored = False
            state.anchored_since = None
            return

        if speed < ANCHORED_SPEED_KN:
            if state.anchored_since is None:
                state.anchored_since = now
            elif (now - state.anchored_since) >= ANCHORED_MIN_DURATION:
                state.is_anchored = True
        else:
            state.is_anchored = False
            state.anchored_since = None

    def _record_anchorage_to_berth(self, state: PortVesselState,
                                   berth_time: datetime) -> None:
        entry = state.anchorage_entry or state.entered_zone_at
        wait_hours = (berth_time - entry).total_seconds() / 3600
        if wait_hours < MIN_WAIT_HOURS:
            return
        self._wait_records[state.port].append(PortWaitRecord(
            mmsi=state.mmsi,
            ship_name=state.ship_name,
            port=state.port,
            anchorage_entry=entry,
            anchorage_exit=berth_time,
            wait_hours=round(wait_hours, 2),
            exited_to="berth",
        ))

    def _record_departure(self, state: PortVesselState,
                          departure_time: datetime) -> None:
        if state.zone != "anchorage":
            return
        entry = state.anchorage_entry or state.entered_zone_at
        wait_hours = (departure_time - entry).total_seconds() / 3600
        if wait_hours < MIN_WAIT_HOURS:
            return
        self._wait_records[state.port].append(PortWaitRecord(
            mmsi=state.mmsi,
            ship_name=state.ship_name,
            port=state.port,
            anchorage_entry=entry,
            anchorage_exit=departure_time,
            wait_hours=round(wait_hours, 2),
            exited_to="departed",
        ))

    def _compute_baseline(self, unlocode: str, now: datetime) -> float:
        cutoff = now - timedelta(days=BASELINE_WINDOW_DAYS)
        recent = [
            r.wait_hours
            for r in self._wait_records.get(unlocode, [])
            if r.anchorage_exit >= cutoff
        ]
        return median(recent) if recent else 0.0

    def _evict_stale(self, now: datetime) -> None:
        cutoff = now - timedelta(hours=STALE_VESSEL_HOURS)
        stale = [mmsi for mmsi, v in self._vessels.items() if v.last_seen < cutoff]
        for mmsi in stale:
            state = self._vessels.pop(mmsi)
            self._record_departure(state, state.last_seen)


_tracker: PortCongestionTracker | None = None


def _get_tracker() -> PortCongestionTracker:
    global _tracker
    if _tracker is None:
        _tracker = PortCongestionTracker()
    return _tracker


def process_position(pos: dict) -> PortVesselState | None:
    return _get_tracker().process_position(pos)


def get_congestion(unlocode: str) -> PortCongestionSnapshot:
    return _get_tracker().get_congestion(unlocode)


def get_all_congestion() -> dict[str, PortCongestionSnapshot]:
    return _get_tracker().get_all_congestion()
