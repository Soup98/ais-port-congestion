# ais-port-congestion

Backend pipeline that detects port congestion from AIS vessel positions and OpenStreetMap port polygons. Writes a SQLite database plus committed JSON snapshots that a separate frontend replays as a "live" demo.

## What it does

For each target port:

1. Pulls the port's outline from OpenStreetMap (Overpass API) and saves it as GeoJSON.
2. Takes a stream of vessel positions and runs point-in-polygon against the port outlines.
3. When a vessel sits inside a port polygon at under 0.5 knots for more than an hour, it counts as anchored.
4. When the vessel leaves the polygon, the pipeline closes the visit and records the wait time.
5. Per-port wait times are aggregated and written to versioned JSON for the frontend.

Target ports (UN/LOCODE): SGSIN, NLRTM, USLAX, DEHAM, CNSHA.

## Why

I wanted a self-contained portfolio project that combines a real public data source (OSM), a basic geospatial algorithm (ray-casting point-in-polygon with shapely), and a pipeline that produces something a frontend can render without needing a live backend. Deploying servers for a portfolio piece felt like overkill, so the design goal was: run the pipeline locally, commit the JSON output, let GitHub Pages handle the rest.

## How it works

The engine is a small stateful tracker. It walks through vessel positions in timestamp order, and for each position it asks `find_port_zone(lat, lon)`. If the position is inside a port polygon, the vessel is added to the tracker's active set. While in the polygon, the tracker watches speed. Once a vessel has been below 0.5 knots for more than an hour it is flagged as anchored, and the start of that anchorage becomes the wait time origin. When the vessel's next position is outside the polygon, the tracker records a completed visit with the elapsed wait.

After replay finishes, visits are grouped per port and per day. The current mean wait per port is compared against a peer-port baseline (median of the per-port means across the network), and ports more than 50 percent above that baseline are flagged as congested.

The actual algorithm is intentionally simple. Real shipping intelligence products use multi-zone polygons (anchorage versus berth), vessel-type filters, draft and beam constraints, and AIS nav-status fields. Those would all be sensible next steps. This repo stays at the point-in-polygon plus speed-threshold layer because that is enough to demonstrate the idea and small enough to read in one sitting.

## Design decisions

**SQLite over Postgres.** The data fits comfortably in a few MB and the pipeline runs locally. SQLite removes the need for a server and makes the repo cloneable and runnable in under a minute.

**Per-port GeoJSON files committed to the repo.** I wanted the OSM data to be inspectable on geojson.io and to not require an Overpass round-trip on every run. The GeoJSON is the source of truth; the database row is a cache.

**Whole port polygon, not split into anchorage and berth zones.** OSM does not consistently distinguish anchorage from berth, and the original golden-path version of this code used hand-curated zone definitions that I did not want to maintain by hand for five ports. The tradeoff is that "berth entry" detection in this repo is degenerate: a wait closes on polygon exit, not on entering a berth sub-zone. For a demo this is acceptable.

**Peer-port baseline instead of historical baseline.** A single network of five ports cannot supply its own historical comparison without a long observation period, and the synthetic data this project ships with covers seven days. Comparing each port against the median of its peers lets the demo show meaningful congestion deltas immediately, at the cost of being a relative rather than absolute signal.

**Synthetic data instead of live AIS.** Originally this project consumed a CSV dump from a Railway-hosted aisstream consumer I run elsewhere. That subscription expired before I could capture the dump, so the pipeline now ships with a deterministic synthetic generator that produces vessel tracks for engineered congestion states. Every other layer in the pipeline is unchanged: the engine does not know or care that the positions are synthetic. Swapping in real AIS just means writing a different loader that targets the same `vessel_positions` table.

**Idempotent CLIs over a workflow runner.** Each step is its own `python -m ingest.*` command. They are safe to rerun, they commit when they finish, and they print enough output to debug from. No Airflow, no Prefect, no Make. For a five-step pipeline that runs once or twice a day during development, more tooling would have cost more than it gained.

## Challenges encountered

**Overpass API rejected requests without a User-Agent.** The first run returned `406 Not Acceptable` for every query. The Overpass public instance gates anonymous clients more strictly than the docs suggest. Setting `User-Agent: ais-port-congestion/0.1` fixed it. Worth knowing if you hit the same wall.

**Multipolygon port shapes inflated visit counts.** Singapore is not one polygon, it is many islands. When a synthetic vessel approaches from offshore its path enters one polygon segment, exits, enters another, and exits again. Each crossing produced a "visit" of a few minutes, which dragged the per-port mean wait time toward zero. Two fixes together solved it: filter out visits shorter than one hour at aggregation time, and report per-port mean over the whole window rather than the latest day.

**Naive versus aware datetimes.** SQLite stores datetimes as naive strings. SQLAlchemy returns them naive on read. The synthetic generator produced timezone-aware UTC timestamps. The idempotency check, which compares `(mmsi, timestamp)` tuples between generated rows and DB rows, was silently failing on timezone mismatch and inserting duplicates. The fix was small (strip tzinfo before insert) but the symptom was confusing because Python equality between naive and aware datetimes raises an exception rather than returning False, and SQLAlchemy was swallowing that path.

**Anchoring synthetic timestamps to a fixed boundary.** The first version of the generator used `datetime.now()` as the upper bound of the time window, which meant successive runs produced slightly different timestamps for the "same" vessel track, breaking the idempotency check. Flooring to the start of the current UTC day made reruns within a day a no-op.

## Data sources

**OpenStreetMap via Overpass API.** Port polygons are pulled from OSM using the tags `harbour=*`, `landuse=harbour`, and `industrial=port`. Geometry is unioned across all matching ways and relations inside a hardcoded bbox per port. Data copyright is OpenStreetMap contributors under ODbL.

**Vessel positions.** Synthetic in this repo. The intended source is aisstream.io (free tier), which provides a live AIS WebSocket. Coverage on the free tier is uneven, with notable gaps in some regions of the world, so do not expect every port to populate in a real run.

## How to run

```bash
pip install -e .
python -m ingest.db_init           # create SQLite schema
python -m ingest.osm_ports         # pull port polygons from OSM
python -m ingest.generate_synthetic # write synthetic vessel positions
python -m ingest.run               # compute port visits + snapshots
python -m ingest.export            # write JSON to snapshots/
pytest                             # run smoke test
```

Each step is idempotent. You can rerun any of them without resetting state.

## Snapshot file shapes

All files are wrapped in a versioned envelope:

```json
{ "version": 1, "generated_at": "2026-05-13T00:00:00+00:00", "data": [ ... ] }
```

`snapshots/ports.json`: one entry per port with the GeoJSON polygon, centroid, current mean wait, peer-port baseline, delta percent, vessel visit count, and a boolean `is_congested` flag.

`snapshots/vessel_positions_sample.json`: up to 5000 positions from the last 24 hours, ordered by timestamp, for the frontend to replay.

`snapshots/port_visits.json`: completed visits from the last 7 days, with anchorage entry, departure, wait time, and status.

## Repo layout

```
ais-port-congestion/
  data/port_geofences/    GeoJSON, one file per port (committed)
  db/                     SQLAlchemy models + engine
  ingest/                 CLI steps
  snapshots/              versioned JSON output (committed)
  tests/                  pytest smoke test
```

## License

Code: MIT. Port polygons derived from OpenStreetMap data are ODbL.
