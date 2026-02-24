#!/usr/bin/env python3
"""
Ingest AIS vessel positions into PostGIS.

This script pulls AIS-like data from an HTTP JSON API or a local JSON file and
stores:
- Latest vessel position in the `vessels` table (including inside/outside MPA state)
- Short-term history in `vessel_positions`
- MPA entry events in `mpa_violations` (entry-only semantics)

Configuration
-------------
- Environment variables:
  - AIS_API_URL: HTTP URL returning JSON data OR a local file path.
  - AIS_API_KEY: Optional API key; if set, sent as header `Authorization: Bearer <key>`.

Expected JSON shape
-------------------
The API response should be either:
- A JSON array: [ { ...vessel objects... } ]
- Or an object with one of: { "vessels": [...]} / { "results": [...]} / { "data": [...] }

Each vessel object should contain at least:
- mmsi (or MMSI)
- lat / latitude (or LAT)
- lon / longitude (or LON)

Optional fields:
- name / shipname / SHIPNAME
- timestamp / ts / time_utc  (ISO-8601; if absent/unparseable, current time is used)

Usage
-----
AISHub (California coast only; set AISHUB_USERNAME in .env):
    docker compose exec backend python scripts/ingest_ais.py --source aishub
    docker compose exec backend python scripts/ingest_ais.py --source aishub --loop --interval 60
    docker compose exec backend python scripts/ingest_ais.py --source aishub --interval-minutes 30

Generic URL or file (set AIS_API_URL or pass --source):
    docker compose exec backend python scripts/ingest_ais.py
    docker compose exec backend python scripts/ingest_ais.py --source scripts/sample_ais.json
    docker compose exec backend python scripts/ingest_ais.py --loop --interval 60
"""

import argparse
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import httpx
import psycopg2
from psycopg2.extensions import connection as PGConnection

# Allow importing database.DATABASE_URL
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from database import DATABASE_URL  # type: ignore


@dataclass
class VesselRecord:
    mmsi: str
    lat: float
    lon: float
    ts: datetime
    name: Optional[str] = None


def _parse_timestamp(raw: Any) -> datetime:
    """Parse a timestamp string or return now() if parsing fails/absent."""
    if not raw:
        return datetime.now(timezone.utc)
    if isinstance(raw, (int, float)):
        # Treat as UNIX seconds
        return datetime.fromtimestamp(float(raw), tz=timezone.utc)
    if isinstance(raw, str):
        txt = raw.strip()
        # Normalize common 'Z' suffix
        if txt.endswith("Z"):
            txt = txt[:-1] + "+00:00"
        try:
            return datetime.fromisoformat(txt)
        except Exception:
            return datetime.now(timezone.utc)
    return datetime.now(timezone.utc)


def _first(*values: Any) -> Any:
    for v in values:
        if v is not None:
            return v
    return None


def normalize_record(raw: Dict[str, Any]) -> Optional[VesselRecord]:
    """Map a provider-specific record into VesselRecord, or None if invalid."""
    mmsi = _first(
        raw.get("mmsi"),
        raw.get("MMSI"),
        raw.get("id"),
    )
    if mmsi is None:
        return None

    lat = _first(
        raw.get("lat"),
        raw.get("latitude"),
        raw.get("LAT"),
        raw.get("LATITUDE"),
    )
    lon = _first(
        raw.get("lon"),
        raw.get("lng"),
        raw.get("longitude"),
        raw.get("LON"),
        raw.get("LONGITUDE"),
    )
    if lat is None or lon is None:
        return None

    try:
        lat_f = float(lat)
        lon_f = float(lon)
    except (TypeError, ValueError):
        return None

    name = _first(
        raw.get("name"),
        raw.get("shipname"),
        raw.get("SHIPNAME"),
        raw.get("NAME"),
    )
    ts = _parse_timestamp(
        _first(
            raw.get("timestamp"),
            raw.get("ts"),
            raw.get("time_utc"),
            raw.get("time"),
            raw.get("TIME"),
        )
    )

    return VesselRecord(mmsi=str(mmsi), lat=lat_f, lon=lon_f, ts=ts, name=name)


def extract_records(payload: Any) -> List[Dict[str, Any]]:
    """Extract list-like payloads from common API response shapes."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("vessels", "results", "data"):
            v = payload.get(key)
            if isinstance(v, list):
                return v
        # AISHub-style: dict with numeric keys, e.g. {"0": {...}, "1": {...}}
        vals = list(payload.values())
        if vals and all(isinstance(x, dict) and ("MMSI" in x or "mmsi" in x) for x in vals):
            return vals
    return []


# California coast bounding box (AISHub lat/lon parameters)
CA_LATMIN = 32.0
CA_LATMAX = 42.5
CA_LONMIN = -125.0
CA_LONMAX = -114.0


def build_aishub_ca_url(
    username: str,
    *,
    format_human: int = 1,
    output: str = "json",
    compress: int = 0,
    interval_minutes: Optional[int] = None,
) -> str:
    """Build AISHub API URL for California coast only (human-readable JSON, no compression)."""
    params = {
        "username": username,
        "format": format_human,
        "output": output,
        "compress": compress,
        "latmin": CA_LATMIN,
        "latmax": CA_LATMAX,
        "lonmin": CA_LONMIN,
        "lonmax": CA_LONMAX,
    }
    if interval_minutes is not None:
        params["interval"] = interval_minutes
    q = "&".join(f"{k}={v}" for k, v in params.items())
    return f"https://data.aishub.net/ws.php?{q}"


def fetch_aishub_ca(
    username: Optional[str] = None,
    interval_minutes: Optional[int] = 60,
) -> List[VesselRecord]:
    """Fetch AISHub data for California coast and return normalized VesselRecords."""
    user = username or os.getenv("AISHUB_USERNAME")
    if not user:
        raise ValueError("AISHUB_USERNAME env var or username argument required for AISHub")
    url = build_aishub_ca_url(user, interval_minutes=interval_minutes)
    raw = fetch_raw(url)
    objs = extract_records(raw)
    records: List[VesselRecord] = []
    for obj in objs:
        if not isinstance(obj, dict):
            continue
        rec = normalize_record(obj)
        if rec:
            records.append(rec)
    return records


def fetch_raw(source: str, api_key: Optional[str] = None) -> Any:
    """Fetch raw JSON from HTTP URL or local file."""
    if source.startswith("http://") or source.startswith("https://"):
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        with httpx.Client(timeout=30.0, follow_redirects=True) as client:
            r = client.get(source, headers=headers)
            r.raise_for_status()
            return r.json()
    # Treat as local file path
    path = Path(source)
    with path.open() as f:
        return json.load(f)


def load_vessel_records(source: str) -> List[VesselRecord]:
    api_key = os.getenv("AIS_API_KEY")
    raw = fetch_raw(source, api_key=api_key)
    objs = extract_records(raw)
    records: List[VesselRecord] = []
    for obj in objs:
        if not isinstance(obj, dict):
            continue
        rec = normalize_record(obj)
        if rec:
            records.append(rec)
    return records


def ensure_core_schema(conn: PGConnection) -> None:
    """
    Ensure the new schema bits exist (idempotent).
    This is defensive for environments where only db/init.sql was applied partially.
    """
    with conn.cursor() as cur:
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
            """
            CREATE INDEX IF NOT EXISTS idx_vessel_positions_geom
                ON vessel_positions USING GIST (geom);
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_vessel_positions_mmsi_ts
                ON vessel_positions (mmsi, ts);
            """
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
            """
            CREATE INDEX IF NOT EXISTS idx_mpa_violations_zone_entry
                ON mpa_violations (zone_id, entry_ts);
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_mpa_violations_mmsi_entry
                ON mpa_violations (mmsi, entry_ts);
            """
        )
    conn.commit()


def process_batch(conn: PGConnection, records: Iterable[VesselRecord]) -> int:
    """
    Store a batch of VesselRecord items:
    - Upsert into vessels (so the row exists for FK from vessel_positions)
    - Insert into vessel_positions
    - Detect outside->inside transitions and insert into mpa_violations
    Returns number of processed records.
    """
    count = 0
    with conn.cursor() as cur:
        for rec in records:
            point_sql = "ST_SetSRID(ST_MakePoint(%s, %s), 4326)"
            # Find zones containing this point
            cur.execute(
                f"""
                SELECT id
                FROM zones
                WHERE ST_Intersects(geom, {point_sql})
                ORDER BY id
                """,
                (rec.lon, rec.lat),
            )
            zone_rows = cur.fetchall()
            zone_ids = [zr[0] for zr in zone_rows] if zone_rows else []
            inside_now = bool(zone_ids)

            # Fetch previous inside state (if any)
            cur.execute(
                "SELECT last_inside, last_zone_ids FROM vessels WHERE mmsi = %s",
                (rec.mmsi,),
            )
            prev = cur.fetchone()
            prev_inside = bool(prev[0]) if prev and prev[0] is not None else False
            prev_zone_ids = set(prev[1] or []) if prev and prev[1] is not None else set()

            # Upsert into vessels first (vessel_positions has FK to vessels)
            cur.execute(
                """
                INSERT INTO vessels (mmsi, name, last_lat, last_lon, last_ts, last_inside, last_zone_ids)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (mmsi) DO UPDATE SET
                    name = COALESCE(EXCLUDED.name, vessels.name),
                    last_lat = EXCLUDED.last_lat,
                    last_lon = EXCLUDED.last_lon,
                    last_ts = EXCLUDED.last_ts,
                    last_inside = EXCLUDED.last_inside,
                    last_zone_ids = EXCLUDED.last_zone_ids;
                """,
                (rec.mmsi, rec.name or rec.mmsi, rec.lat, rec.lon, rec.ts, inside_now, zone_ids or None),
            )

            # Insert historical position
            cur.execute(
                f"""
                INSERT INTO vessel_positions (mmsi, ts, lat, lon, geom)
                VALUES (%s, %s, %s, %s, {point_sql})
                """,
                (rec.mmsi, rec.ts, rec.lat, rec.lon, rec.lon, rec.lat),
            )

            # Entry-only violation detection: outside -> inside transition
            if not prev_inside and inside_now:
                for zid in zone_ids:
                    cur.execute(
                        """
                        INSERT INTO mpa_violations (mmsi, zone_id, entry_ts, source)
                        VALUES (%s, %s, %s, %s)
                        """,
                        (rec.mmsi, zid, rec.ts, "AIS_API"),
                    )

            count += 1
    conn.commit()
    return count


def run_once(source: str) -> None:
    records = load_vessel_records(source)
    if not records:
        print("No valid AIS records found from source:", source)
        return

    conn = psycopg2.connect(DATABASE_URL)
    try:
        ensure_core_schema(conn)
        processed = process_batch(conn, records)
        print(f"Processed {processed} AIS records from {source}")
    finally:
        conn.close()


def run_once_aishub(interval_minutes: Optional[int] = 60) -> None:
    """Fetch AISHub data for California coast and ingest."""
    records = fetch_aishub_ca(interval_minutes=interval_minutes)
    if not records:
        print("No AISHub records returned for California coast")
        return

    conn = psycopg2.connect(DATABASE_URL)
    try:
        ensure_core_schema(conn)
        processed = process_batch(conn, records)
        print(f"Processed {processed} AISHub records (California coast)")
    finally:
        conn.close()


def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="Ingest AIS vessel positions into PostGIS.")
    parser.add_argument(
        "--source",
        help="HTTP URL, local JSON path, or 'aishub' for AISHub California coast. Defaults to AIS_API_URL or AISHUB_USERNAME.",
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        help="Continuously poll the AIS source.",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=60,
        help="Polling interval in seconds when --loop is set (default: 60).",
    )
    parser.add_argument(
        "--interval-minutes",
        type=int,
        default=60,
        help="AISHub only: max age of positions in minutes (default: 60).",
    )
    args = parser.parse_args(argv)

    source = args.source or os.getenv("AIS_API_URL")
    use_aishub = source and source.lower() == "aishub"
    if not source and os.getenv("AISHUB_USERNAME"):
        use_aishub = True

    if use_aishub:
        if not args.loop:
            run_once_aishub(interval_minutes=args.interval_minutes)
            return
        import time
        print(f"Starting AISHub ingestion loop (California coast), interval={args.interval}s")
        while True:
            try:
                run_once_aishub(interval_minutes=args.interval_minutes)
            except Exception as e:
                print("Error in ingestion loop:", e)
            time.sleep(max(1, args.interval))
        return

    if not source:
        raise SystemExit("AIS_API_URL or AISHUB_USERNAME env var, or --source (URL/file/aishub), required")

    if not args.loop:
        run_once(source)
        return

    # Looping mode
    import time
    print(f"Starting AIS ingestion loop. Source={source}, interval={args.interval}s")
    while True:
        try:
            run_once(source)
        except Exception as e:
            print("Error in ingestion loop:", e)
        time.sleep(max(1, args.interval))


if __name__ == "__main__":
    main()

