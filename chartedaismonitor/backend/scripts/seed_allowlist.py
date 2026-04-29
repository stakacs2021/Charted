#!/usr/bin/env python3
"""
Bulk-load the institutional allowlist from `allowlist_seed.csv`.

Each row becomes a wildcard `(mmsi, zone_id=NULL, category, note)` entry in
`mpa_violation_allowlist`, suppressing violations for that MMSI in every zone.
This is the right tool for vessels whose JOB is to be in MPAs (USCG, CDFW
patrol, NOAA research, harbor pilots).

For per-(mmsi, zone) exceptions (e.g. one specific charter permit) keep using
SQL as documented in README.

Usage
-----
    docker compose exec backend python scripts/seed_allowlist.py
    docker compose exec backend python scripts/seed_allowlist.py --csv path/to/file.csv --dry-run
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

import psycopg2

_backend = Path(__file__).resolve().parent.parent
if str(_backend) not in sys.path:
    sys.path.insert(0, str(_backend))
from database import DATABASE_URL  # type: ignore


def load_csv(path: Path) -> List[Tuple[str, Optional[str], Optional[str]]]:
    """Return list of (mmsi, category, note). Skips comments / blanks / header row."""
    out: List[Tuple[str, Optional[str], Optional[str]]] = []
    if not path.exists():
        return out
    with path.open() as f:
        reader = csv.reader(f)
        for row in reader:
            if not row:
                continue
            first = row[0].strip()
            if not first or first.startswith("#"):
                continue
            if first.lower() == "mmsi":
                continue  # header
            mmsi = first
            category = row[1].strip() if len(row) >= 2 and row[1].strip() else None
            note = row[2].strip() if len(row) >= 3 and row[2].strip() else None
            out.append((mmsi, category, note))
    return out


def upsert(conn, rows: Iterable[Tuple[str, Optional[str], Optional[str]]], *, dry_run: bool) -> int:
    rows = list(rows)
    print(f"Allowlist plan: {len(rows)} wildcard entries (dry_run={dry_run})")
    if dry_run or not rows:
        return len(rows)
    with conn.cursor() as cur:
        for mmsi, category, note in rows:
            # Use the partial unique index `mpa_allowlist_wild` for ON CONFLICT.
            cur.execute(
                """
                INSERT INTO mpa_violation_allowlist (mmsi, zone_id, note, category)
                VALUES (%s, NULL, %s, %s)
                ON CONFLICT (mmsi) WHERE zone_id IS NULL
                DO UPDATE SET note = EXCLUDED.note, category = EXCLUDED.category;
                """,
                (mmsi, note, category),
            )
    conn.commit()
    return len(rows)


def main(argv: Iterable[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--csv",
        default=str(Path(__file__).resolve().parent / "allowlist_seed.csv"),
        help="Path to the CSV (default: alongside this script)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print count, don't write")
    args = parser.parse_args(list(argv) if argv is not None else None)

    rows = load_csv(Path(args.csv))
    conn = psycopg2.connect(DATABASE_URL)
    try:
        n = upsert(conn, rows, dry_run=args.dry_run)
        print(f"Done. {n} rows {'planned' if args.dry_run else 'upserted'}.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
