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
            INSERT INTO vessels (mmsi, name, last_lat, last_lon, last_ts)
            VALUES (%s, %s, %s, %s, NOW())
            ON CONFLICT (mmsi) DO UPDATE SET
                name = COALESCE(EXCLUDED.name, vessels.name),
                last_lat = EXCLUDED.last_lat,
                last_lon = EXCLUDED.last_lon,
                last_ts = NOW()
            """,
            (body.mmsi, body.name or body.mmsi, body.lat, body.lon),
        )
    return {"mmsi": body.mmsi, "lat": body.lat, "lon": body.lon, "updated": True}


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
