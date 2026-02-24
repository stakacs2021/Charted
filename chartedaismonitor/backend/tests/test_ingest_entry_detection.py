import os
from datetime import datetime, timezone

import pytest

psycopg2 = pytest.importorskip("psycopg2", reason="psycopg2-binary not installed; run: pip install -r requirements.txt")

from scripts.ingest_ais import VesselRecord, ensure_core_schema, process_batch


def _db_url() -> str:
    url = os.getenv("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL must be set for integration tests")
    return url


def test_outside_to_inside_creates_single_entry_violation():
    """
    Integration test against PostGIS:
    - Create a tiny test zone (square around (0,0) .. (1,1))
    - Ingest one position outside, then one inside
    - Ensure exactly one mpa_violations entry is recorded (entry-only semantics)
    """
    conn = psycopg2.connect(_db_url())
    try:
        ensure_core_schema(conn)
        with conn.cursor() as cur:
            # Create a test zone
            cur.execute(
                """
                INSERT INTO zones (name, designation, geom, source)
                VALUES (
                    'TEST_ZONE',
                    'TEST',
                    ST_GeomFromText('POLYGON((0 0, 1 0, 1 1, 0 1, 0 0))', 4326)::geometry(MULTIPOLYGON, 4326),
                    'tests'
                )
                RETURNING id
                """
            )
            zone_id = cur.fetchone()[0]
        conn.commit()

        mmsi = "test-mmsi-001"
        ts0 = datetime(2026, 2, 8, 0, 0, 0, tzinfo=timezone.utc)
        ts1 = datetime(2026, 2, 8, 0, 10, 0, tzinfo=timezone.utc)

        # Outside then inside
        batch = [
            VesselRecord(mmsi=mmsi, lat=2.0, lon=2.0, ts=ts0, name="Test Vessel"),
            VesselRecord(mmsi=mmsi, lat=0.5, lon=0.5, ts=ts1, name="Test Vessel"),
        ]
        process_batch(conn, batch)

        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM mpa_violations WHERE mmsi = %s AND zone_id = %s",
                (mmsi, zone_id),
            )
            count = cur.fetchone()[0]

        assert count == 1
    finally:
        # Best-effort cleanup
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM mpa_violations WHERE mmsi = 'test-mmsi-001'")
                cur.execute("DELETE FROM vessel_positions WHERE mmsi = 'test-mmsi-001'")
                cur.execute("DELETE FROM vessels WHERE mmsi = 'test-mmsi-001'")
                cur.execute("DELETE FROM zones WHERE name = 'TEST_ZONE' AND source = 'tests'")
            conn.commit()
        except Exception:
            conn.rollback()
        conn.close()

