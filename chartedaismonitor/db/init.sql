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
    last_ts TIMESTAMPTZ
);

-- Optional: index for vessel lookups by MMSI (pk already gives this)
-- No extra indexes needed for minimal usage.
