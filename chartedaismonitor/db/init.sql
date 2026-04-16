-- PostGIS extension (required for geometry types and spatial queries)
CREATE EXTENSION IF NOT EXISTS postgis;

-- California MPA zones (boundaries)
CREATE TABLE IF NOT EXISTS zones (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    designation TEXT,
    geom GEOMETRY(MULTIPOLYGON, 4326) NOT NULL,
    -- Stable classification used for policy rules (computed in app and persisted)
    bracket_class TEXT,
    bracket_source TEXT,
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
    last_zone_ids INTEGER[],
    -- Enrichment fields (may be populated by ingest)
    country TEXT,
    callsign TEXT,
    country_iso2 TEXT,
    cog DOUBLE PRECISION,
    true_heading DOUBLE PRECISION,
    bearing_deg DOUBLE PRECISION,
    -- Vessel-type policy inputs
    ais_ship_type_code INTEGER,
    vessel_type TEXT
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

-- Per-zone policy: which vessel types are allowed in which bracket_class (designation category).
-- If a row is missing, the default behavior is "NOT allowed" unless explicitly allowlisted.
CREATE TABLE IF NOT EXISTS zone_vessel_type_policy (
    zone_bracket_class TEXT NOT NULL,
    vessel_type TEXT NOT NULL,
    allowed BOOLEAN NOT NULL,
    note TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (zone_bracket_class, vessel_type)
);

-- Resolve whether a vessel is allowed to be in a zone, combining:
-- 1) explicit per-(mmsi, zone_id) allowlist override
-- 2) bracket_class + vessel_type policy
CREATE OR REPLACE FUNCTION is_vessel_allowed_in_zone(p_mmsi TEXT, p_zone_id INTEGER)
RETURNS BOOLEAN
LANGUAGE plpgsql
AS $$
DECLARE
    v_allowed BOOLEAN;
    v_vessel_type TEXT;
    v_bracket TEXT;
BEGIN
    -- Explicit allowlist override
    IF EXISTS (
        SELECT 1 FROM mpa_violation_allowlist
        WHERE mmsi = p_mmsi AND zone_id = p_zone_id
    ) THEN
        RETURN TRUE;
    END IF;

    SELECT vessel_type INTO v_vessel_type
    FROM vessels
    WHERE mmsi = p_mmsi;

    SELECT bracket_class INTO v_bracket
    FROM zones
    WHERE id = p_zone_id;

    IF v_vessel_type IS NULL OR v_bracket IS NULL THEN
        RETURN FALSE;
    END IF;

    SELECT allowed INTO v_allowed
    FROM zone_vessel_type_policy
    WHERE zone_bracket_class = v_bracket
      AND vessel_type = v_vessel_type;

    -- Default deny when policy row missing
    RETURN COALESCE(v_allowed, FALSE);
END;
$$;

-- Trigger: suppress inserting violations for allowed vessels.
CREATE OR REPLACE FUNCTION mpa_violations_enforce_vessel_type_policy()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF is_vessel_allowed_in_zone(NEW.mmsi, NEW.zone_id) THEN
        RETURN NULL;
    END IF;
    RETURN NEW;
END;
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger WHERE tgname = 'trg_mpa_violations_policy'
    ) THEN
        CREATE TRIGGER trg_mpa_violations_policy
        BEFORE INSERT ON mpa_violations
        FOR EACH ROW
        EXECUTE FUNCTION mpa_violations_enforce_vessel_type_policy();
    END IF;
END
$$;
