-- PostGIS extension (required for geometry types and spatial queries)
CREATE EXTENSION IF NOT EXISTS postgis;

-- California MPA zones (boundaries)
CREATE TABLE IF NOT EXISTS zones (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    designation TEXT,
    geom GEOMETRY(MULTIPOLYGON, 4326) NOT NULL,
    source TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Spatial index for fast point-in-polygon and bounding-box queries
CREATE INDEX IF NOT EXISTS idx_zones_geom ON zones USING GIST (geom);

-- Vessels (latest position for testing)
CREATE TABLE IF NOT EXISTS vessels (
    mmsi TEXT PRIMARY KEY,
    name TEXT,
    last_lat DOUBLE PRECISION,
    last_lon DOUBLE PRECISION,
    last_ts TIMESTAMPTZ,
    -- Whether the vessel was inside any MPA on last update (for entry detection)
    last_inside BOOLEAN,
    -- IDs of zones the vessel was last known to be inside (for multi-zone entries)
    last_zone_ids INTEGER[]
);

-- Optional: index for vessel lookups by MMSI (pk already gives this)
-- No extra indexes needed for minimal usage.

-- Historical vessel positions (short-term trails)
CREATE TABLE IF NOT EXISTS vessel_positions (
    id SERIAL PRIMARY KEY,
    mmsi TEXT NOT NULL REFERENCES vessels (mmsi) ON DELETE CASCADE,
    ts TIMESTAMPTZ NOT NULL,
    lat DOUBLE PRECISION NOT NULL,
    lon DOUBLE PRECISION NOT NULL,
    geom GEOMETRY(POINT, 4326) NOT NULL
);

-- Spatial index for vessel positions
CREATE INDEX IF NOT EXISTS idx_vessel_positions_geom
    ON vessel_positions USING GIST (geom);

-- Lookup positions per vessel over time
CREATE INDEX IF NOT EXISTS idx_vessel_positions_mmsi_ts
    ON vessel_positions (mmsi, ts);

-- MPA entry events (violations)
CREATE TABLE IF NOT EXISTS mpa_violations (
    id SERIAL PRIMARY KEY,
    mmsi TEXT NOT NULL REFERENCES vessels (mmsi) ON DELETE CASCADE,
    zone_id INTEGER NOT NULL REFERENCES zones (id) ON DELETE CASCADE,
    entry_ts TIMESTAMPTZ NOT NULL,
    exit_ts TIMESTAMPTZ,
    source TEXT
);

CREATE INDEX IF NOT EXISTS idx_mpa_violations_zone_entry
    ON mpa_violations (zone_id, entry_ts);

CREATE INDEX IF NOT EXISTS idx_mpa_violations_mmsi_entry
    ON mpa_violations (mmsi, entry_ts);

-- Optional: per-(mmsi, zone) allowlist — ingest skips inserting mpa_violations for these pairs (editable live via SQL).
CREATE TABLE IF NOT EXISTS mpa_violation_allowlist (
    mmsi TEXT NOT NULL,
    zone_id INTEGER NOT NULL REFERENCES zones (id) ON DELETE CASCADE,
    note TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (mmsi, zone_id)
);
