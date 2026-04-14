import os
from datetime import datetime, timezone

import pytest

psycopg2 = pytest.importorskip("psycopg2", reason="psycopg2-binary not installed; run: pip install -r requirements.txt")

from scripts.ingest_ais import ensure_core_schema


def _db_url() -> str:
    url = os.getenv("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL must be set for integration tests")
    return url


def test_history_query_shape():
    """
    Lightweight DB integration test:
    - Ensure schema exists
    - Insert a zone + an entry violation
    - Verify the joinable fields exist and can be selected
    """
    conn = psycopg2.connect(_db_url())
    try:
        ensure_core_schema(conn)
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO zones (name, designation, geom, source)
                VALUES (
                    'TEST_ZONE_HISTORY',
                    'TEST',
                    ST_GeomFromText('POLYGON((0 0, 1 0, 1 1, 0 1, 0 0))', 4326)::geometry(MULTIPOLYGON, 4326),
                    'tests'
                )
                RETURNING id
                """
            )
            zone_id = cur.fetchone()[0]
            cur.execute(
                """
                INSERT INTO vessels (mmsi, name, last_lat, last_lon, last_ts, last_inside, last_zone_ids)
                VALUES (%s, %s, %s, %s, NOW(), %s, %s)
                ON CONFLICT (mmsi) DO NOTHING
                """,
                ("test-mmsi-history-001", "Test Vessel", 0.5, 0.5, True, [zone_id]),
            )
            entry_ts = datetime(2026, 4, 13, 12, 0, 0, tzinfo=timezone.utc)
            cur.execute(
                """
                INSERT INTO mpa_violations (mmsi, zone_id, entry_ts, source)
                VALUES (%s, %s, %s, %s)
                RETURNING id
                """,
                ("test-mmsi-history-001", zone_id, entry_ts, "tests"),
            )
            violation_id = cur.fetchone()[0]
        conn.commit()

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT mv.id AS violation_id, mv.mmsi, mv.zone_id, z.name AS zone_name, mv.entry_ts, mv.source
                FROM mpa_violations mv
                JOIN zones z ON z.id = mv.zone_id
                WHERE mv.id = %s
                """,
                (violation_id,),
            )
            row = cur.fetchone()
        assert row is not None
        assert row[0] == violation_id
        assert row[3] == "TEST_ZONE_HISTORY"
    finally:
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM mpa_violations WHERE mmsi = 'test-mmsi-history-001'")
                cur.execute("DELETE FROM vessel_positions WHERE mmsi = 'test-mmsi-history-001'")
                cur.execute("DELETE FROM vessels WHERE mmsi = 'test-mmsi-history-001'")
                cur.execute("DELETE FROM zones WHERE name = 'TEST_ZONE_HISTORY' AND source = 'tests'")
            conn.commit()
        except Exception:
            conn.rollback()
        conn.close()

