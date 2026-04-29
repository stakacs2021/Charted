import os
from datetime import datetime, timedelta, timezone

import pytest

psycopg2 = pytest.importorskip("psycopg2", reason="psycopg2-binary not installed; run: pip install -r requirements.txt")

from scripts import ingest_ais as mod
from scripts.ingest_ais import VesselRecord, ensure_core_schema, process_batch


def _db_url() -> str:
    url = os.getenv("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL must be set for integration tests")
    return url


@pytest.fixture
def conn():
    """Provide a connection with schema ensured. Caller cleans up its own test rows."""
    c = psycopg2.connect(_db_url())
    try:
        ensure_core_schema(c)
        yield c
    finally:
        c.close()


@pytest.fixture
def test_zone(conn):
    """Create a 1x1-degree square zone with TEST designation; clean up after."""
    name = "TEST_ZONE"
    with conn.cursor() as cur:
        cur.execute("DELETE FROM zones WHERE name = %s AND source = 'tests'", (name,))
        cur.execute(
            """
            INSERT INTO zones (name, designation, geom, source)
            VALUES (
                %s, 'TEST',
                ST_GeomFromText('POLYGON((0 0, 1 0, 1 1, 0 1, 0 0))', 4326)::geometry(MULTIPOLYGON, 4326),
                'tests'
            )
            RETURNING id
            """,
            (name,),
        )
        zone_id = cur.fetchone()[0]
    conn.commit()
    yield zone_id
    with conn.cursor() as cur:
        cur.execute("DELETE FROM mpa_violations WHERE zone_id = %s", (zone_id,))
        cur.execute("DELETE FROM zones WHERE id = %s", (zone_id,))
    conn.commit()


@pytest.fixture
def cleanup_mmsis(conn):
    """Fixture that records test MMSIs and tears them down."""
    mmsis: list[str] = []
    yield mmsis
    if not mmsis:
        return
    with conn.cursor() as cur:
        for m in mmsis:
            cur.execute("DELETE FROM mpa_violations WHERE mmsi = %s", (m,))
            cur.execute("DELETE FROM vessel_positions WHERE mmsi = %s", (m,))
            cur.execute("DELETE FROM mpa_violation_allowlist WHERE mmsi = %s", (m,))
            cur.execute("DELETE FROM vessels WHERE mmsi = %s", (m,))
    conn.commit()


@pytest.fixture
def permissive_heuristics(monkeypatch):
    """Disable speed/dwell/buffer gates so we can isolate entry-detection logic."""
    monkeypatch.setattr(mod, "SPEED_TRANSIT_KN", 9999.0)
    monkeypatch.setattr(mod, "MIN_DWELL_SECONDS", 0)
    monkeypatch.setattr(mod, "BOUNDARY_BUFFER_M", 0.0)


def _violation_count(conn, mmsi: str, zone_id: int) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM mpa_violations WHERE mmsi = %s AND zone_id = %s",
            (mmsi, zone_id),
        )
        return int(cur.fetchone()[0])


def test_outside_to_inside_creates_single_entry_violation(
    conn, test_zone, cleanup_mmsis, permissive_heuristics
):
    """Baseline entry-detection (heuristics disabled): outside -> inside = one violation."""
    mmsi = "test-mmsi-001"
    cleanup_mmsis.append(mmsi)
    ts0 = datetime(2026, 2, 8, 0, 0, 0, tzinfo=timezone.utc)
    ts1 = datetime(2026, 2, 8, 0, 10, 0, tzinfo=timezone.utc)
    batch = [
        VesselRecord(mmsi=mmsi, lat=2.0, lon=2.0, ts=ts0, name="Test Vessel"),
        VesselRecord(mmsi=mmsi, lat=0.5, lon=0.5, ts=ts1, name="Test Vessel"),
    ]
    process_batch(conn, batch)
    assert _violation_count(conn, mmsi, test_zone) == 1


def test_high_speed_transit_does_not_violate(
    conn, test_zone, cleanup_mmsis, monkeypatch
):
    """SOG above SPEED_TRANSIT_KN is treated as transit -> no violation."""
    monkeypatch.setattr(mod, "SPEED_TRANSIT_KN", 5.0)
    monkeypatch.setattr(mod, "MIN_DWELL_SECONDS", 0)
    monkeypatch.setattr(mod, "BOUNDARY_BUFFER_M", 0.0)

    mmsi = "test-mmsi-speed"
    cleanup_mmsis.append(mmsi)
    ts0 = datetime(2026, 2, 8, 0, 0, 0, tzinfo=timezone.utc)
    ts1 = datetime(2026, 2, 8, 0, 10, 0, tzinfo=timezone.utc)
    batch = [
        VesselRecord(mmsi=mmsi, lat=2.0, lon=2.0, ts=ts0, name="Speedy", sog=12.0),
        VesselRecord(mmsi=mmsi, lat=0.5, lon=0.5, ts=ts1, name="Speedy", sog=12.0),
    ]
    process_batch(conn, batch)
    assert _violation_count(conn, mmsi, test_zone) == 0


def test_short_dwell_does_not_violate(
    conn, test_zone, cleanup_mmsis, monkeypatch
):
    """Single inside fix below MIN_DWELL_SECONDS does not record a violation."""
    monkeypatch.setattr(mod, "SPEED_TRANSIT_KN", 9999.0)
    monkeypatch.setattr(mod, "MIN_DWELL_SECONDS", 120)
    monkeypatch.setattr(mod, "BOUNDARY_BUFFER_M", 0.0)

    mmsi = "test-mmsi-dwell-short"
    cleanup_mmsis.append(mmsi)
    ts0 = datetime(2026, 2, 8, 0, 0, 0, tzinfo=timezone.utc)
    ts1 = datetime(2026, 2, 8, 0, 0, 30, tzinfo=timezone.utc)
    batch = [
        VesselRecord(mmsi=mmsi, lat=2.0, lon=2.0, ts=ts0, name="Brief"),
        VesselRecord(mmsi=mmsi, lat=0.5, lon=0.5, ts=ts1, name="Brief"),
    ]
    process_batch(conn, batch)
    assert _violation_count(conn, mmsi, test_zone) == 0


def test_long_dwell_records_violation(
    conn, test_zone, cleanup_mmsis, monkeypatch
):
    """Inside long enough -> deferred violation fires once."""
    monkeypatch.setattr(mod, "SPEED_TRANSIT_KN", 9999.0)
    monkeypatch.setattr(mod, "MIN_DWELL_SECONDS", 60)
    monkeypatch.setattr(mod, "BOUNDARY_BUFFER_M", 0.0)

    mmsi = "test-mmsi-dwell-long"
    cleanup_mmsis.append(mmsi)
    ts0 = datetime(2026, 2, 8, 0, 0, 0, tzinfo=timezone.utc)
    ts1 = datetime(2026, 2, 8, 0, 0, 30, tzinfo=timezone.utc)  # leading edge, deferred
    ts2 = datetime(2026, 2, 8, 0, 5, 0, tzinfo=timezone.utc)   # dwell satisfied
    ts3 = datetime(2026, 2, 8, 0, 8, 0, tzinfo=timezone.utc)   # still inside, no double-fire
    batch = [
        VesselRecord(mmsi=mmsi, lat=2.0, lon=2.0, ts=ts0, name="Loiter"),
        VesselRecord(mmsi=mmsi, lat=0.5, lon=0.5, ts=ts1, name="Loiter"),
        VesselRecord(mmsi=mmsi, lat=0.6, lon=0.6, ts=ts2, name="Loiter"),
        VesselRecord(mmsi=mmsi, lat=0.7, lon=0.7, ts=ts3, name="Loiter"),
    ]
    process_batch(conn, batch)
    assert _violation_count(conn, mmsi, test_zone) == 1


def test_type_policy_allows_passenger_in_smr_via_unknown_bucket(
    conn, test_zone, cleanup_mmsis, permissive_heuristics
):
    """
    With seeded policy: a 'passenger' vessel in an SMR-equivalent bracket should
    be suppressed by the trigger (transit is legal). We seed a single bracket row
    so the test is hermetic from whatever's already in zone_vessel_type_policy.
    """
    # Reclassify the test zone to NoTake by inserting a name-based exception
    with conn.cursor() as cur:
        cur.execute("UPDATE zones SET bracket_class = 'NoTake' WHERE id = %s", (test_zone,))
        cur.execute(
            """
            INSERT INTO zone_vessel_type_policy (zone_bracket_class, vessel_type, allowed, note)
            VALUES ('NoTake', 'passenger', TRUE, 'test-passenger-allowed')
            ON CONFLICT (zone_bracket_class, vessel_type) DO UPDATE SET allowed = TRUE;
            """
        )
    conn.commit()

    mmsi = "test-mmsi-passenger"
    cleanup_mmsis.append(mmsi)
    ts0 = datetime(2026, 2, 8, 0, 0, 0, tzinfo=timezone.utc)
    ts1 = datetime(2026, 2, 8, 0, 10, 0, tzinfo=timezone.utc)
    batch = [
        VesselRecord(mmsi=mmsi, lat=2.0, lon=2.0, ts=ts0, name="Cruise Liner",
                     vessel_type="passenger", bucket_source="ais"),
        VesselRecord(mmsi=mmsi, lat=0.5, lon=0.5, ts=ts1, name="Cruise Liner",
                     vessel_type="passenger", bucket_source="ais"),
    ]
    process_batch(conn, batch)
    assert _violation_count(conn, mmsi, test_zone) == 0


def test_type_policy_denies_fishing_in_smr(
    conn, test_zone, cleanup_mmsis, permissive_heuristics
):
    """A fishing vessel in NoTake should still record a violation."""
    with conn.cursor() as cur:
        cur.execute("UPDATE zones SET bracket_class = 'NoTake' WHERE id = %s", (test_zone,))
        cur.execute(
            """
            INSERT INTO zone_vessel_type_policy (zone_bracket_class, vessel_type, allowed, note)
            VALUES ('NoTake', 'fishing', FALSE, 'test-fishing-denied')
            ON CONFLICT (zone_bracket_class, vessel_type) DO UPDATE SET allowed = FALSE;
            """
        )
    conn.commit()

    mmsi = "test-mmsi-fishing"
    cleanup_mmsis.append(mmsi)
    ts0 = datetime(2026, 2, 8, 0, 0, 0, tzinfo=timezone.utc)
    ts1 = datetime(2026, 2, 8, 0, 10, 0, tzinfo=timezone.utc)
    batch = [
        VesselRecord(mmsi=mmsi, lat=2.0, lon=2.0, ts=ts0, name="Trawler",
                     vessel_type="fishing", bucket_source="ais"),
        VesselRecord(mmsi=mmsi, lat=0.5, lon=0.5, ts=ts1, name="Trawler",
                     vessel_type="fishing", bucket_source="ais"),
    ]
    process_batch(conn, batch)
    assert _violation_count(conn, mmsi, test_zone) == 1


def test_wildcard_allowlist_suppresses_violation(
    conn, test_zone, cleanup_mmsis, permissive_heuristics
):
    """A wildcard (zone_id IS NULL) allowlist row covers all zones."""
    mmsi = "test-mmsi-wildcard"
    cleanup_mmsis.append(mmsi)
    with conn.cursor() as cur:
        cur.execute("UPDATE zones SET bracket_class = 'NoTake' WHERE id = %s", (test_zone,))
        cur.execute(
            """
            INSERT INTO zone_vessel_type_policy (zone_bracket_class, vessel_type, allowed, note)
            VALUES ('NoTake', 'fishing', FALSE, 'test-fishing-denied')
            ON CONFLICT (zone_bracket_class, vessel_type) DO UPDATE SET allowed = FALSE;
            """
        )
        cur.execute(
            """
            INSERT INTO mpa_violation_allowlist (mmsi, zone_id, note, category)
            VALUES (%s, NULL, 'test-wildcard', 'government')
            ON CONFLICT (mmsi) WHERE zone_id IS NULL DO UPDATE SET note = EXCLUDED.note;
            """,
            (mmsi,),
        )
    conn.commit()

    ts0 = datetime(2026, 2, 8, 0, 0, 0, tzinfo=timezone.utc)
    ts1 = datetime(2026, 2, 8, 0, 10, 0, tzinfo=timezone.utc)
    # Use fishing+NoTake which would otherwise deny -- proves wildcard wins
    batch = [
        VesselRecord(mmsi=mmsi, lat=2.0, lon=2.0, ts=ts0, name="USCGC Wildcard",
                     vessel_type="fishing", bucket_source="ais"),
        VesselRecord(mmsi=mmsi, lat=0.5, lon=0.5, ts=ts1, name="USCGC Wildcard",
                     vessel_type="fishing", bucket_source="ais"),
    ]
    process_batch(conn, batch)
    assert _violation_count(conn, mmsi, test_zone) == 0


def test_unknown_vessel_type_treated_as_unknown_bucket(
    conn, test_zone, cleanup_mmsis, permissive_heuristics
):
    """NULL vessel_type collapses to 'unknown' bucket; permissive default = no violation."""
    with conn.cursor() as cur:
        cur.execute("UPDATE zones SET bracket_class = 'NoTake' WHERE id = %s", (test_zone,))
        cur.execute(
            """
            INSERT INTO zone_vessel_type_policy (zone_bracket_class, vessel_type, allowed, note)
            VALUES ('NoTake', 'unknown', TRUE, 'test-unknown-allowed')
            ON CONFLICT (zone_bracket_class, vessel_type) DO UPDATE SET allowed = TRUE;
            """
        )
    conn.commit()

    mmsi = "test-mmsi-unknown"
    cleanup_mmsis.append(mmsi)
    ts0 = datetime(2026, 2, 8, 0, 0, 0, tzinfo=timezone.utc)
    ts1 = datetime(2026, 2, 8, 0, 10, 0, tzinfo=timezone.utc)
    batch = [
        VesselRecord(mmsi=mmsi, lat=2.0, lon=2.0, ts=ts0, name="Mystery Boat"),
        VesselRecord(mmsi=mmsi, lat=0.5, lon=0.5, ts=ts1, name="Mystery Boat"),
    ]
    process_batch(conn, batch)
    assert _violation_count(conn, mmsi, test_zone) == 0

