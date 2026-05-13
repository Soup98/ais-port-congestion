from sqlalchemy import (
    Column, Integer, String, Float, DateTime, UniqueConstraint, Index,
)
from .engine import Base


class VesselPosition(Base):
    __tablename__ = "vessel_positions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    mmsi = Column(String, nullable=False, index=True)
    imo = Column(String, nullable=True)
    vessel_name = Column(String, nullable=True)
    vessel_type = Column(String, nullable=True)
    lat = Column(Float, nullable=False)
    lng = Column(Float, nullable=False)
    speed_knots = Column(Float, nullable=True)
    course = Column(Float, nullable=True)
    timestamp = Column(DateTime, nullable=False, index=True)

    __table_args__ = (
        UniqueConstraint("mmsi", "timestamp", name="uq_vessel_position_mmsi_ts"),
        Index("ix_vessel_positions_mmsi_ts", "mmsi", "timestamp"),
    )


class PortGeofence(Base):
    __tablename__ = "port_geofences"

    locode = Column(String, primary_key=True)
    name = Column(String, nullable=False)
    polygon_geojson = Column(String, nullable=False)
    centroid_lat = Column(Float, nullable=False)
    centroid_lng = Column(Float, nullable=False)
    source = Column(String, nullable=False)  # "osm" | "manual_bbox"


class PortVisit(Base):
    __tablename__ = "port_visits"

    id = Column(Integer, primary_key=True, autoincrement=True)
    mmsi = Column(String, nullable=False, index=True)
    locode = Column(String, nullable=False, index=True)
    anchorage_entry = Column(DateTime, nullable=True)
    berth_entry = Column(DateTime, nullable=True)
    departure = Column(DateTime, nullable=True)
    wait_time_hours = Column(Float, nullable=True)
    status = Column(String, nullable=False)  # anchored | berthed | departed

    __table_args__ = (
        Index("ix_port_visits_locode_mmsi", "locode", "mmsi"),
    )


class CongestionSnapshot(Base):
    __tablename__ = "congestion_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    locode = Column(String, nullable=False, index=True)
    snapshot_date = Column(DateTime, nullable=False, index=True)
    vessels_anchored = Column(Integer, nullable=False, default=0)
    mean_wait_hours = Column(Float, nullable=True)
    baseline_wait_hours = Column(Float, nullable=True)
    delta_pct = Column(Float, nullable=True)

    __table_args__ = (
        UniqueConstraint("locode", "snapshot_date", name="uq_snapshot_locode_date"),
    )
