"""
AIS MPA Monitor API: zones (MPAs) and vessel-in-zone checks.
"""
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel
from typing import Optional
import asyncio
import json

from database import get_cursor
from zone_classification import classify_bracket

app = FastAPI(title="AIS MPA Monitor")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def ensure_extended_schema():
    """
    Ensure schema additions exist for trails + violations.
    Note: docker init scripts only run on a fresh DB volume, so we keep this
    idempotent to avoid 'missing table/column' issues during development.
    """
    with get_cursor() as cur:
        cur.execute(
            """
            ALTER TABLE vessels
            ADD COLUMN IF NOT EXISTS last_inside BOOLEAN,
            ADD COLUMN IF NOT EXISTS last_zone_ids INTEGER[],
            ADD COLUMN IF NOT EXISTS country TEXT,
            ADD COLUMN IF NOT EXISTS callsign TEXT,
            ADD COLUMN IF NOT EXISTS country_iso2 TEXT,
            ADD COLUMN IF NOT EXISTS cog DOUBLE PRECISION,
            ADD COLUMN IF NOT EXISTS true_heading DOUBLE PRECISION,
            ADD COLUMN IF NOT EXISTS bearing_deg DOUBLE PRECISION,
            ADD COLUMN IF NOT EXISTS ais_ship_type_code INTEGER,
            ADD COLUMN IF NOT EXISTS vessel_type TEXT;
            """
        )
        cur.execute(
            """
            ALTER TABLE zones
            ADD COLUMN IF NOT EXISTS bracket_class TEXT,
            ADD COLUMN IF NOT EXISTS bracket_source TEXT;
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS vessel_positions (
                id SERIAL PRIMARY KEY,
                mmsi TEXT NOT NULL REFERENCES vessels (mmsi) ON DELETE CASCADE,
                ts TIMESTAMPTZ NOT NULL,
                lat DOUBLE PRECISION NOT NULL,
                lon DOUBLE PRECISION NOT NULL,
                geom GEOMETRY(POINT, 4326) NOT NULL
            );
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_vessel_positions_geom ON vessel_positions USING GIST (geom);"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_vessel_positions_mmsi_ts ON vessel_positions (mmsi, ts);"
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS mpa_violations (
                id SERIAL PRIMARY KEY,
                mmsi TEXT NOT NULL REFERENCES vessels (mmsi) ON DELETE CASCADE,
                zone_id INTEGER NOT NULL REFERENCES zones (id) ON DELETE CASCADE,
                entry_ts TIMESTAMPTZ NOT NULL,
                exit_ts TIMESTAMPTZ,
                source TEXT
            );
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_mpa_violations_zone_entry ON mpa_violations (zone_id, entry_ts);"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_mpa_violations_mmsi_entry ON mpa_violations (mmsi, entry_ts);"
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS mpa_violation_allowlist (
                mmsi TEXT NOT NULL,
                zone_id INTEGER NOT NULL REFERENCES zones (id) ON DELETE CASCADE,
                note TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (mmsi, zone_id)
            );
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS zone_vessel_type_policy (
                zone_bracket_class TEXT NOT NULL,
                vessel_type TEXT NOT NULL,
                allowed BOOLEAN NOT NULL,
                note TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (zone_bracket_class, vessel_type)
            );
            """
        )
        cur.execute(
            """
            CREATE OR REPLACE FUNCTION is_vessel_allowed_in_zone(p_mmsi TEXT, p_zone_id INTEGER)
            RETURNS BOOLEAN
            LANGUAGE plpgsql
            AS $$
            DECLARE
                v_allowed BOOLEAN;
                v_vessel_type TEXT;
                v_bracket TEXT;
            BEGIN
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

                RETURN COALESCE(v_allowed, FALSE);
            END;
            $$;
            """
        )
        cur.execute(
            """
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
            """
        )
        cur.execute(
            """
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
            """
        )

        # Backfill bracket_class/bracket_source for existing zones (idempotent)
        cur.execute(
            "SELECT id, name, designation FROM zones WHERE bracket_class IS NULL OR bracket_source IS NULL"
        )
        zone_rows = cur.fetchall()
        for r in zone_rows:
            bracket = classify_bracket(designation=r["designation"], zone_id=r["id"], name=r["name"])
            cur.execute(
                """
                UPDATE zones
                SET bracket_class = %s,
                    bracket_source = %s
                WHERE id = %s
                """,
                (bracket.bracket_class, bracket.bracket_source, r["id"]),
            )


@app.get("/")
def root():
    return {"status": "AIS MPA Monitor running"}


@app.get("/debug/stats")
def debug_stats():
    """Debug endpoint: check zone count and database status."""
    with get_cursor() as cur:
        cur.execute("SELECT COUNT(*) as count FROM zones")
        zone_count = cur.fetchone()["count"]
        
        cur.execute("SELECT COUNT(*) as count FROM vessels")
        vessel_count = cur.fetchone()["count"]
        
        # Check PostGIS extension
        cur.execute("SELECT PostGIS_version() as version")
        postgis_version = cur.fetchone()["version"]
    
    return {
        "zone_count": zone_count,
        "vessel_count": vessel_count,
        "postgis_version": postgis_version,
    }


@app.get("/debug/nearby")
def debug_nearby(
    lat: float = Query(..., description="Latitude"),
    lon: float = Query(..., description="Longitude"),
    distance_km: float = Query(10.0, description="Distance in kilometers"),
):
    """Debug endpoint: find zones within distance_km of a point."""
    with get_cursor() as cur:
        # Find zones within distance (using geography for accurate distance)
        cur.execute(
            """
            SELECT id, name, designation,
                   ST_Distance(
                       ST_GeogFromText(ST_AsText(geom)),
                       ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography
                   ) / 1000.0 AS distance_km
            FROM zones
            WHERE ST_DWithin(
                ST_GeogFromText(ST_AsText(geom)),
                ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography,
                %s * 1000.0
            )
            ORDER BY distance_km
            LIMIT 10
            """,
            (lon, lat, lon, lat, distance_km),
        )
        rows = cur.fetchall()
    
    return {
        "lat": lat,
        "lon": lon,
        "distance_km": distance_km,
        "zones_nearby": [
            {
                "id": r["id"],
                "name": r["name"],
                "designation": r["designation"],
                "distance_km": round(r["distance_km"], 3),
            }
            for r in rows
        ],
    }


# ---------- Feature 1: Zones (MPAs) as GeoJSON ----------

@app.get("/zones")
def get_zones():
    """
    Returns all MPA zones as a single GeoJSON FeatureCollection (for map rendering).
    """
    with get_cursor() as cur:
        cur.execute("""
            SELECT id, name, designation, source,
                   ST_AsGeoJSON(geom)::json AS geom_json
            FROM zones
            ORDER BY id
        """)
        rows = cur.fetchall()

    features = []
    for r in rows:
        bracket = classify_bracket(designation=r["designation"], zone_id=r["id"], name=r["name"])
        features.append({
            "type": "Feature",
            "id": r["id"],
            "properties": {
                "id": r["id"],
                "name": r["name"],
                "designation": r["designation"],
                "source": r["source"],
                "bracket_class": bracket.bracket_class,
                "bracket_source": bracket.bracket_source,
            },
            "geometry": r["geom_json"],
        })

    fc = {"type": "FeatureCollection", "features": features}
    return JSONResponse(content=fc, media_type="application/geo+json")


@app.get("/zones/{zone_id}/stats")
def zone_stats(zone_id: int):
    """Return basic MPA metadata plus violation count (entry-only)."""
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT z.id, z.name, z.designation, z.source,
                   COALESCE(v.violation_count, 0) AS violation_count,
                   v.last_violation_ts
            FROM zones z
            LEFT JOIN (
                SELECT zone_id,
                       COUNT(*)::int AS violation_count,
                       MAX(entry_ts) AS last_violation_ts
                FROM mpa_violations
                GROUP BY zone_id
            ) v ON v.zone_id = z.id
            WHERE z.id = %s
            """,
            (zone_id,),
        )
        row = cur.fetchone()
    if not row:
        return {"error": "Zone not found", "id": zone_id}
    return row


@app.get("/zones/with-stats")
def zones_with_stats():
    """Zones as GeoJSON with aggregated violation_count property."""
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT z.id, z.name, z.designation, z.source,
                   ST_AsGeoJSON(z.geom)::json AS geom_json,
                   COALESCE(v.violation_count, 0) AS violation_count
            FROM zones z
            LEFT JOIN (
                SELECT zone_id, COUNT(*)::int AS violation_count
                FROM mpa_violations
                GROUP BY zone_id
            ) v ON v.zone_id = z.id
            ORDER BY z.id
            """
        )
        rows = cur.fetchall()

    features = []
    for r in rows:
        bracket = classify_bracket(designation=r["designation"], zone_id=r["id"], name=r["name"])
        features.append(
            {
                "type": "Feature",
                "id": r["id"],
                "properties": {
                    "id": r["id"],
                    "name": r["name"],
                    "designation": r["designation"],
                    "source": r["source"],
                    "violation_count": r["violation_count"],
                    "bracket_class": bracket.bracket_class,
                    "bracket_source": bracket.bracket_source,
                },
                "geometry": r["geom_json"],
            }
        )

    fc = {"type": "FeatureCollection", "features": features}
    return JSONResponse(content=fc, media_type="application/geo+json")


# ---------- Feature 2: Point-in-zone and vessels ----------

@app.get("/inside")
def check_inside(
    lat: float = Query(..., description="Latitude"),
    lon: float = Query(..., description="Longitude"),
):
    """
    Check if a (lat, lon) point lies inside any MPA.
    Uses ST_Intersects so points on the boundary count as inside (consistent with "in or on").
    """
    with get_cursor() as cur:
        # ST_Intersects: point on boundary returns true. ST_Contains would exclude boundary.
        # For "is the vessel inside an MPA?" we want boundary = inside for safety/consistency.
        cur.execute(
            """
            SELECT id, name
            FROM zones
            WHERE ST_Intersects(geom, ST_SetSRID(ST_MakePoint(%s, %s), 4326))
            ORDER BY id
            """,
            (lon, lat),  # PostGIS: (x,y) = (lon, lat)
        )
        rows = cur.fetchall()

    matched = [{"id": r["id"], "name": r["name"]} for r in rows]
    return {
        "lat": lat,
        "lon": lon,
        "inside": len(matched) > 0,
        "matched_zones": matched,
    }


class VesselUpdate(BaseModel):
    mmsi: str
    name: Optional[str] = None
    lat: float
    lon: float


@app.post("/vessels/update")
def update_vessel(body: VesselUpdate):
    """Update a vessel's latest position (for testing)."""
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT id
            FROM zones
            WHERE ST_Intersects(geom, ST_SetSRID(ST_MakePoint(%s, %s), 4326))
            ORDER BY id
            """,
            (body.lon, body.lat),
        )
        zone_ids = [r["id"] for r in cur.fetchall()]
        inside_now = len(zone_ids) > 0

        cur.execute(
            """
            INSERT INTO vessels (mmsi, name, last_lat, last_lon, last_ts, last_inside, last_zone_ids)
            VALUES (%s, %s, %s, %s, NOW(), %s, %s)
            ON CONFLICT (mmsi) DO UPDATE SET
                name = COALESCE(EXCLUDED.name, vessels.name),
                last_lat = EXCLUDED.last_lat,
                last_lon = EXCLUDED.last_lon,
                last_ts = NOW(),
                last_inside = EXCLUDED.last_inside,
                last_zone_ids = EXCLUDED.last_zone_ids
            """,
            (body.mmsi, body.name or body.mmsi, body.lat, body.lon, inside_now, zone_ids or None),
        )
        cur.execute(
            """
            INSERT INTO vessel_positions (mmsi, ts, lat, lon, geom)
            VALUES (%s, NOW(), %s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326))
            """,
            (body.mmsi, body.lat, body.lon, body.lon, body.lat),
        )
    return {"mmsi": body.mmsi, "lat": body.lat, "lon": body.lon, "updated": True}


@app.get("/vessels/live")
def vessels_live(
    min_lat: float = Query(32.0, description="Bounding box min latitude"),
    min_lon: float = Query(-125.0, description="Bounding box min longitude"),
    max_lat: float = Query(42.5, description="Bounding box max latitude"),
    max_lon: float = Query(-114.0, description="Bounding box max longitude"),
    limit: int = Query(200, ge=1, le=5000, description="Max vessels to return"),
    include_zones: bool = Query(False, description="Include matched zone ids per vessel"),
):
    """Current vessel snapshots for map rendering."""
    with get_cursor() as cur:
        if include_zones:
            cur.execute(
                """
                SELECT v.mmsi, v.name, v.country, v.country_iso2, v.callsign, v.cog, v.true_heading, v.bearing_deg,
                       v.last_lat, v.last_lon, v.last_ts,
                       COALESCE((
                           SELECT array_agg(z.id ORDER BY z.id)
                           FROM zones z
                           WHERE ST_Intersects(z.geom, ST_SetSRID(ST_MakePoint(v.last_lon, v.last_lat), 4326))
                       ), ARRAY[]::int[]) AS matched_zone_ids,
                       EXISTS(SELECT 1 FROM mpa_violations WHERE mmsi = v.mmsi) AS has_mpa_violations
                FROM vessels v
                WHERE v.last_lat IS NOT NULL AND v.last_lon IS NOT NULL
                  AND v.last_lat BETWEEN %s AND %s
                  AND v.last_lon BETWEEN %s AND %s
                ORDER BY v.last_ts DESC NULLS LAST
                LIMIT %s
                """,
                (min_lat, max_lat, min_lon, max_lon, limit),
            )
            rows = cur.fetchall()
            return [
                {
                    "mmsi": r["mmsi"],
                    "name": r["name"],
                    "country": r["country"],
                    "country_iso2": r["country_iso2"],
                    "callsign": r["callsign"],
                    "cog": r["cog"],
                    "true_heading": r["true_heading"],
                    "bearing_deg": r["bearing_deg"],
                    "lat": r["last_lat"],
                    "lon": r["last_lon"],
                    "last_ts": r["last_ts"],
                    "inside_any_mpa": len(r["matched_zone_ids"]) > 0,
                    "matched_zone_ids": r["matched_zone_ids"],
                    "has_mpa_violations": bool(r["has_mpa_violations"]),
                }
                for r in rows
            ]

        cur.execute(
            """
            SELECT v.mmsi, v.name, v.country, v.country_iso2, v.callsign, v.cog, v.true_heading, v.bearing_deg,
                   v.last_lat, v.last_lon, v.last_ts,
                   EXISTS(
                       SELECT 1
                       FROM zones z
                       WHERE ST_Intersects(z.geom, ST_SetSRID(ST_MakePoint(v.last_lon, v.last_lat), 4326))
                   ) AS inside_any_mpa,
                   EXISTS(SELECT 1 FROM mpa_violations WHERE mmsi = v.mmsi) AS has_mpa_violations
            FROM vessels v
            WHERE v.last_lat IS NOT NULL AND v.last_lon IS NOT NULL
              AND v.last_lat BETWEEN %s AND %s
              AND v.last_lon BETWEEN %s AND %s
            ORDER BY v.last_ts DESC NULLS LAST
            LIMIT %s
            """,
            (min_lat, max_lat, min_lon, max_lon, limit),
        )
        rows = cur.fetchall()

    return [
        {
            "mmsi": r["mmsi"],
            "name": r["name"],
            "country": r["country"],
            "country_iso2": r["country_iso2"],
            "callsign": r["callsign"],
            "cog": r["cog"],
            "true_heading": r["true_heading"],
            "bearing_deg": r["bearing_deg"],
            "lat": r["last_lat"],
            "lon": r["last_lon"],
            "last_ts": r["last_ts"],
            "inside_any_mpa": bool(r["inside_any_mpa"]),
            "has_mpa_violations": bool(r["has_mpa_violations"]),
        }
        for r in rows
    ]


@app.get("/vessels/asof")
def vessels_asof(
    ts: str = Query(..., description="As-of timestamp (ISO-8601)"),
    min_lat: float = Query(32.0, description="Bounding box min latitude"),
    min_lon: float = Query(-125.0, description="Bounding box min longitude"),
    max_lat: float = Query(42.5, description="Bounding box max latitude"),
    max_lon: float = Query(-114.0, description="Bounding box max longitude"),
    limit: int = Query(200, ge=1, le=5000, description="Max vessels to return"),
    include_zones: bool = Query(False, description="Include matched zone ids per vessel"),
):
    """
    Historical vessel snapshots as-of a timestamp.

    Uses vessel_positions history to pick the last known position per vessel at/before `ts`.
    Note: metadata (name/country/type) comes from `vessels` (latest-known), while position comes from history.
    """
    with get_cursor() as cur:
        if include_zones:
            cur.execute(
                """
                WITH last_pos AS (
                    SELECT DISTINCT ON (vp.mmsi)
                           vp.mmsi, vp.ts AS last_ts, vp.lat AS last_lat, vp.lon AS last_lon
                    FROM vessel_positions vp
                    WHERE vp.ts <= %s::timestamptz
                    ORDER BY vp.mmsi, vp.ts DESC
                )
                SELECT v.mmsi, v.name, v.country, v.country_iso2, v.callsign, v.cog, v.true_heading, v.bearing_deg,
                       v.ais_ship_type_code, v.vessel_type,
                       lp.last_lat, lp.last_lon, lp.last_ts,
                       COALESCE((
                           SELECT array_agg(z.id ORDER BY z.id)
                           FROM zones z
                           WHERE ST_Intersects(z.geom, ST_SetSRID(ST_MakePoint(lp.last_lon, lp.last_lat), 4326))
                       ), ARRAY[]::int[]) AS matched_zone_ids,
                       EXISTS(
                           SELECT 1 FROM mpa_violations mv
                           WHERE mv.mmsi = v.mmsi AND mv.entry_ts <= %s::timestamptz
                       ) AS has_mpa_violations
                FROM last_pos lp
                JOIN vessels v ON v.mmsi = lp.mmsi
                WHERE lp.last_lat BETWEEN %s AND %s
                  AND lp.last_lon BETWEEN %s AND %s
                ORDER BY lp.last_ts DESC NULLS LAST
                LIMIT %s
                """,
                (ts, ts, min_lat, max_lat, min_lon, max_lon, limit),
            )
            rows = cur.fetchall()
            return [
                {
                    "mmsi": r["mmsi"],
                    "name": r["name"],
                    "country": r["country"],
                    "country_iso2": r["country_iso2"],
                    "callsign": r["callsign"],
                    "cog": r["cog"],
                    "true_heading": r["true_heading"],
                    "bearing_deg": r["bearing_deg"],
                    "ais_ship_type_code": r["ais_ship_type_code"],
                    "vessel_type": r["vessel_type"],
                    "lat": r["last_lat"],
                    "lon": r["last_lon"],
                    "last_ts": r["last_ts"],
                    "inside_any_mpa": len(r["matched_zone_ids"]) > 0,
                    "matched_zone_ids": r["matched_zone_ids"],
                    "has_mpa_violations": bool(r["has_mpa_violations"]),
                }
                for r in rows
            ]

        cur.execute(
            """
            WITH last_pos AS (
                SELECT DISTINCT ON (vp.mmsi)
                       vp.mmsi, vp.ts AS last_ts, vp.lat AS last_lat, vp.lon AS last_lon
                FROM vessel_positions vp
                WHERE vp.ts <= %s::timestamptz
                ORDER BY vp.mmsi, vp.ts DESC
            )
            SELECT v.mmsi, v.name, v.country, v.country_iso2, v.callsign, v.cog, v.true_heading, v.bearing_deg,
                   v.ais_ship_type_code, v.vessel_type,
                   lp.last_lat, lp.last_lon, lp.last_ts,
                   EXISTS(
                       SELECT 1
                       FROM zones z
                       WHERE ST_Intersects(z.geom, ST_SetSRID(ST_MakePoint(lp.last_lon, lp.last_lat), 4326))
                   ) AS inside_any_mpa,
                   EXISTS(
                       SELECT 1 FROM mpa_violations mv
                       WHERE mv.mmsi = v.mmsi AND mv.entry_ts <= %s::timestamptz
                   ) AS has_mpa_violations
            FROM last_pos lp
            JOIN vessels v ON v.mmsi = lp.mmsi
            WHERE lp.last_lat BETWEEN %s AND %s
              AND lp.last_lon BETWEEN %s AND %s
            ORDER BY lp.last_ts DESC NULLS LAST
            LIMIT %s
            """,
            (ts, ts, min_lat, max_lat, min_lon, max_lon, limit),
        )
        rows = cur.fetchall()

    return [
        {
            "mmsi": r["mmsi"],
            "name": r["name"],
            "country": r["country"],
            "country_iso2": r["country_iso2"],
            "callsign": r["callsign"],
            "cog": r["cog"],
            "true_heading": r["true_heading"],
            "bearing_deg": r["bearing_deg"],
            "ais_ship_type_code": r["ais_ship_type_code"],
            "vessel_type": r["vessel_type"],
            "lat": r["last_lat"],
            "lon": r["last_lon"],
            "last_ts": r["last_ts"],
            "inside_any_mpa": bool(r["inside_any_mpa"]),
            "has_mpa_violations": bool(r["has_mpa_violations"]),
        }
        for r in rows
    ]


@app.get("/vessels/leaderboard")
def vessels_leaderboard(
    limit: int = Query(50, ge=1, le=200, description="Max vessels to return"),
):
    """Top violators by MPA entry count (leaderboard)."""
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT v.mmsi, v.name, v.country, v.country_iso2, v.callsign,
                   COUNT(mv.id)::int AS violation_count,
                   MAX(mv.entry_ts) AS last_violation_ts
            FROM vessels v
            JOIN mpa_violations mv ON mv.mmsi = v.mmsi
            GROUP BY v.mmsi, v.name, v.country, v.country_iso2, v.callsign
            ORDER BY violation_count DESC
            LIMIT %s
            """,
            (limit,),
        )
        rows = cur.fetchall()

    return [
        {
            "rank": i + 1,
            "mmsi": r["mmsi"],
            "name": r["name"],
            "country": r["country"],
            "country_iso2": r["country_iso2"],
            "callsign": r["callsign"],
            "violation_count": r["violation_count"],
            "last_violation_ts": r["last_violation_ts"],
        }
        for i, r in enumerate(rows)
    ]


@app.get("/vessels/{mmsi}/trail")
def vessel_trail(
    mmsi: str,
    hours: int = Query(6, ge=1, le=72, description="How many hours of trail to return"),
    limit: int = Query(1000, ge=1, le=5000, description="Max positions to return"),
    end_ts: Optional[str] = Query(None, description="Trail end timestamp (ISO-8601). Defaults to now()"),
):
    """Short-term vessel trail from historical positions (for dotted path rendering)."""
    with get_cursor() as cur:
        if end_ts:
            cur.execute(
                """
                SELECT ts, lat, lon
                FROM (
                    SELECT ts, lat, lon
                    FROM vessel_positions
                    WHERE mmsi = %s
                      AND ts <= %s::timestamptz
                      AND ts >= (%s::timestamptz - (%s * INTERVAL '1 hour'))
                    ORDER BY ts DESC
                    LIMIT %s
                ) t
                ORDER BY ts ASC
                """,
                (mmsi, end_ts, end_ts, hours, limit),
            )
        else:
            cur.execute(
                """
                SELECT ts, lat, lon
                FROM (
                    SELECT ts, lat, lon
                    FROM vessel_positions
                    WHERE mmsi = %s
                      AND ts >= NOW() - (%s * INTERVAL '1 hour')
                    ORDER BY ts DESC
                    LIMIT %s
                ) t
                ORDER BY ts ASC
                """,
                (mmsi, hours, limit),
            )
        rows = cur.fetchall()

    positions = [{"ts": r["ts"], "lat": r["lat"], "lon": r["lon"]} for r in rows]
    coords = [[p["lon"], p["lat"]] for p in positions]

    line_feature = None
    if len(coords) >= 2:
        line_feature = {
            "type": "Feature",
            "properties": {"mmsi": mmsi},
            "geometry": {"type": "LineString", "coordinates": coords},
        }

    return {"mmsi": mmsi, "hours": hours, "count": len(positions), "positions": positions, "line": line_feature}


@app.get("/vessels/{mmsi}/inside")
def vessel_inside(mmsi: str):
    """Return whether this vessel's last position is inside any MPA and which ones."""
    with get_cursor() as cur:
        cur.execute(
            "SELECT last_lat, last_lon FROM vessels WHERE mmsi = %s",
            (mmsi,),
        )
        row = cur.fetchone()
    if not row or row["last_lat"] is None or row["last_lon"] is None:
        return {"mmsi": mmsi, "error": "Vessel not found or has no position", "inside": False, "matched_zones": []}

    lat, lon = row["last_lat"], row["last_lon"]
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT id, name
            FROM zones
            WHERE ST_Intersects(geom, ST_SetSRID(ST_MakePoint(%s, %s), 4326))
            ORDER BY id
            """,
            (lon, lat),
        )
        zones = cur.fetchall()

    matched = [{"id": r["id"], "name": r["name"]} for r in zones]
    return {
        "mmsi": mmsi,
        "last_lat": lat,
        "last_lon": lon,
        "inside": len(matched) > 0,
        "matched_zones": matched,
    }


# ---------- Feature 4: ingest-runtime history timeline (MPA entry events) ----------

@app.get("/history/mpa-entries")
def history_mpa_entries(
    limit: int = Query(100, ge=1, le=1000, description="Max entries to return"),
    since_ts: Optional[str] = Query(None, description="Only include entries on/after this timestamp (ISO-8601)"),
):
    """
    Recent MPA entry events (mpa_violations rows), newest-first.

    `since_ts` is treated as a timestamp string and passed to Postgres for parsing.
    """
    with get_cursor() as cur:
        if since_ts:
            cur.execute(
                """
                SELECT mv.id AS violation_id,
                       mv.mmsi,
                       mv.zone_id,
                       z.name AS zone_name,
                       mv.entry_ts,
                       mv.source
                FROM mpa_violations mv
                JOIN zones z ON z.id = mv.zone_id
                WHERE mv.entry_ts >= %s::timestamptz
                ORDER BY mv.entry_ts DESC
                LIMIT %s
                """,
                (since_ts, limit),
            )
        else:
            cur.execute(
                """
                SELECT mv.id AS violation_id,
                       mv.mmsi,
                       mv.zone_id,
                       z.name AS zone_name,
                       mv.entry_ts,
                       mv.source
                FROM mpa_violations mv
                JOIN zones z ON z.id = mv.zone_id
                ORDER BY mv.entry_ts DESC
                LIMIT %s
                """,
                (limit,),
            )
        rows = cur.fetchall()

    return [
        {
            "violation_id": r["violation_id"],
            "mmsi": r["mmsi"],
            "zone_id": r["zone_id"],
            "zone_name": r["zone_name"],
            "entry_ts": r["entry_ts"],
            "source": r["source"],
        }
        for r in rows
    ]


@app.get("/history/mpa-entries/window")
def history_mpa_entries_window(
    start_ts: str = Query(..., description="Window start timestamp (ISO-8601)"),
    end_ts: str = Query(..., description="Window end timestamp (ISO-8601)"),
    limit: int = Query(1000, ge=1, le=5000, description="Max entries to return"),
):
    """MPA entry events within [start_ts, end_ts], newest-first."""
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT mv.id AS violation_id,
                   mv.mmsi,
                   mv.zone_id,
                   z.name AS zone_name,
                   mv.entry_ts,
                   mv.source
            FROM mpa_violations mv
            JOIN zones z ON z.id = mv.zone_id
            WHERE mv.entry_ts >= %s::timestamptz
              AND mv.entry_ts <= %s::timestamptz
            ORDER BY mv.entry_ts DESC
            LIMIT %s
            """,
            (start_ts, end_ts, limit),
        )
        rows = cur.fetchall()

    return [
        {
            "violation_id": r["violation_id"],
            "mmsi": r["mmsi"],
            "zone_id": r["zone_id"],
            "zone_name": r["zone_name"],
            "entry_ts": r["entry_ts"],
            "source": r["source"],
        }
        for r in rows
    ]


@app.get("/events/mpa-entries")
async def events_mpa_entries(
    after_id: int = Query(0, ge=0, description="Only stream entries with id > after_id"),
    poll_ms: int = Query(1000, ge=250, le=5000, description="DB poll interval (milliseconds)"),
):
    """Server-Sent Events stream of new MPA entry events while ingest is running."""

    async def gen():
        last_id = after_id
        # Tell proxies and clients not to buffer.
        yield "retry: 2000\n"
        while True:
            events = []
            with get_cursor() as cur:
                cur.execute(
                    """
                    SELECT mv.id AS violation_id,
                           mv.mmsi,
                           mv.zone_id,
                           z.name AS zone_name,
                           mv.entry_ts,
                           mv.source
                    FROM mpa_violations mv
                    JOIN zones z ON z.id = mv.zone_id
                    WHERE mv.id > %s
                    ORDER BY mv.id ASC
                    LIMIT 200
                    """,
                    (last_id,),
                )
                rows = cur.fetchall()

            for r in rows:
                ev = {
                    "violation_id": r["violation_id"],
                    "mmsi": r["mmsi"],
                    "zone_id": r["zone_id"],
                    "zone_name": r["zone_name"],
                    "entry_ts": r["entry_ts"],
                    "source": r["source"],
                }
                events.append(ev)
                last_id = max(last_id, int(r["violation_id"]))

            for ev in events:
                yield f"data: {json.dumps(ev, default=str)}\n\n"

            await asyncio.sleep(poll_ms / 1000.0)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
