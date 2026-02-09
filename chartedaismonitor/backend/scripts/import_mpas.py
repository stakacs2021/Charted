#!/usr/bin/env python3
"""
Import California MPA boundaries into PostGIS.

Data source: California Marine Protected Areas [ds582], California Open Data (CDFW).
GeoJSON: https://data-cdfw.opendata.arcgis.com/api/download/v1/items/117a99c8745a48c6a48bac70005b1b11/geojson?layers=0

Usage:
  python scripts/import_mpas.py [path_to_local.geojson]
  If no path given, downloads from the URL above.

Uses pure Python + psycopg2 + shapely (no ogr2ogr) so it runs in any environment.
"""
import os
import sys
import json
import httpx
from pathlib import Path

import psycopg2
from shapely.geometry import shape, mapping
from shapely.ops import unary_union
from shapely.validation import make_valid

# Add parent so we can use database.DATABASE_URL
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from database import DATABASE_URL

SOURCE_URL = "https://data-cdfw.opendata.arcgis.com/api/download/v1/items/117a99c8745a48c6a48bac70005b1b11/geojson?layers=0"
SOURCE_LABEL = "California Marine Protected Areas [ds582], CDFW / California Open Data"


def first(*candidates, default=None):
    for c in candidates:
        if c is not None and c != "":
            return c
    return default


def normalize_properties(props):
    """Map various possible GeoJSON property names to our schema."""
    if not props:
        props = {}
    name = first(
        props.get("NAME"),
        props.get("MPA_name"),
        props.get("name"),
        default="Unnamed MPA",
    )
    designation = first(
        props.get("DESIG"),
        props.get("MPA_designation"),
        props.get("designation"),
        props.get("Type"),
        default=None,
    )
    return name, designation


def geom_to_multipolygon(geom):
    """Convert Shapely geometry to MULTIPOLYGON, fixing invalid geometries."""
    if geom is None or geom.is_empty:
        return None
    try:
        g = make_valid(geom)
    except Exception:
        g = geom.buffer(0)  # often fixes self-intersections
    if g is None or g.is_empty:
        return None
    if g.geom_type == "Polygon":
        return g
    if g.geom_type == "MultiPolygon":
        return g
    if g.geom_type == "GeometryCollection":
        polys = [x for x in g.geoms if x.geom_type in ("Polygon", "MultiPolygon")]
        if not polys:
            return None
        return unary_union(polys)
    return None


def load_geojson(path_or_url: str):
    """Load GeoJSON from local file or URL."""
    if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
        with httpx.Client(timeout=120.0, follow_redirects=True) as client:
            r = client.get(path_or_url)
            # Handle 302 redirects manually if follow_redirects didn't work
            if r.status_code == 302 and "Location" in r.headers:
                redirect_url = r.headers["Location"]
                r = client.get(redirect_url)
            r.raise_for_status()
            return r.json()
    with open(path_or_url) as f:
        return json.load(f)


SCHEMA_SQL = """
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE TABLE IF NOT EXISTS zones (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    designation TEXT,
    geom GEOMETRY(MULTIPOLYGON, 4326) NOT NULL,
    source TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_zones_geom ON zones USING GIST (geom);
CREATE TABLE IF NOT EXISTS vessels (
    mmsi TEXT PRIMARY KEY,
    name TEXT,
    last_lat DOUBLE PRECISION,
    last_lon DOUBLE PRECISION,
    last_ts TIMESTAMPTZ
);
"""


def ensure_schema(cur):
    """Ensure zones and vessels tables exist (idempotent)."""
    for stmt in SCHEMA_SQL.strip().split(";"):
        stmt = stmt.strip()
        if stmt:
            cur.execute(stmt)


def run_import(geojson_path_or_url=None):
    url_or_path = geojson_path_or_url or SOURCE_URL
    print("Loading GeoJSON from", url_or_path)
    data = load_geojson(url_or_path)

    features = data.get("features") or []
    print(f"Found {len(features)} features")

    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    cur = conn.cursor()

    ensure_schema(cur)
    conn.commit()

    cur.execute("DELETE FROM zones")
    inserted = 0
    skipped = 0

    for i, f in enumerate(features):
        geom_in = f.get("geometry")
        if not geom_in:
            skipped += 1
            continue
        try:
            shp = shape(geom_in)
        except Exception as e:
            print(f"  Feature {i}: invalid geometry - {e}")
            skipped += 1
            continue
        multi = geom_to_multipolygon(shp)
        if multi is None:
            skipped += 1
            continue
        # PostGIS: ST_GeomFromText + ST_Multi + ST_MakeValid
        name, designation = normalize_properties(f.get("properties") or {})

        try:
            # ST_Multi ensures Polygon becomes MULTIPOLYGON for our table type
            cur.execute(
                """
                INSERT INTO zones (name, designation, geom, source)
                VALUES (%s, %s, ST_MakeValid(ST_Multi(ST_GeomFromText(%s, 4326)))::geometry(MULTIPOLYGON, 4326), %s)
                """,
                (name, designation, multi.wkt, SOURCE_LABEL),
            )
            inserted += 1
        except Exception as e:
            print(f"  Feature {i} ({name}): insert failed - {e}")
            skipped += 1

    conn.commit()

    # Ensure GIST index exists (idempotent)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_zones_geom ON zones USING GIST (geom)")
    conn.commit()

    cur.close()
    conn.close()
    print(f"Done. Inserted: {inserted}, skipped: {skipped}")


if __name__ == "__main__":
    run_import(sys.argv[1] if len(sys.argv) > 1 else None)
