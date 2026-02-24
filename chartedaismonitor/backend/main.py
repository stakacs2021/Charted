"""
AIS MPA Monitor API: zones (MPAs) and vessel-in-zone checks.
"""
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional

from database import get_cursor

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
            ADD COLUMN IF NOT EXISTS last_zone_ids INTEGER[];
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
        features.append({
            "type": "Feature",
            "id": r["id"],
            "properties": {
                "id": r["id"],
                "name": r["name"],
                "designation": r["designation"],
                "source": r["source"],
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
                SELECT v.mmsi, v.name, v.last_lat, v.last_lon, v.last_ts,
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
            SELECT v.mmsi, v.name, v.last_lat, v.last_lon, v.last_ts,
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
            "lat": r["last_lat"],
            "lon": r["last_lon"],
            "last_ts": r["last_ts"],
            "inside_any_mpa": bool(r["inside_any_mpa"]),
            "has_mpa_violations": bool(r["has_mpa_violations"]),
        }
        for r in rows
    ]


@app.get("/vessels/{mmsi}/trail")
def vessel_trail(
    mmsi: str,
    hours: int = Query(6, ge=1, le=72, description="How many hours of trail to return"),
    limit: int = Query(1000, ge=1, le=5000, description="Max positions to return"),
):
    """Short-term vessel trail from historical positions (for dotted path rendering)."""
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT ts, lat, lon
            FROM vessel_positions
            WHERE mmsi = %s
              AND ts >= NOW() - (%s * INTERVAL '1 hour')
            ORDER BY ts ASC
            LIMIT %s
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
