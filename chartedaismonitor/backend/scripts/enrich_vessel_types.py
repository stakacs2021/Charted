#!/usr/bin/env python3
"""
Backfill `vessels.vessel_type` for rows that ingest didn't label, using:

  1. CSV overrides (`vessel_type_overrides.csv`) -- highest priority, manual curation.
  2. Name-regex heuristics -- fast, deterministic, covers institutional fleets
     (USCGC, R/V, F/V, M/V, PILOT, TUG) that frequently send weak AIS static data.

Existing labels with `bucket_source = 'ais'` are NEVER overwritten -- AIS-broadcast
type is treated as authoritative. Other sources (NULL, prior name_regex labels)
can be replaced by a higher-priority source.

Usage
-----
    docker compose exec backend python scripts/enrich_vessel_types.py
    docker compose exec backend python scripts/enrich_vessel_types.py --dry-run
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import psycopg2

_backend = Path(__file__).resolve().parent.parent
if str(_backend) not in sys.path:
    sys.path.insert(0, str(_backend))
from database import DATABASE_URL  # type: ignore


# Source priority. Higher index wins.
_SOURCE_PRIORITY: Dict[str, int] = {
    "name_regex": 1,
    "enrichment_api": 2,
    "csv_override": 3,
    "ais": 4,  # AIS-broadcast static data is authoritative
}


# Ordered name-regex rules: first match wins. Patterns are case-insensitive.
# Prefixes like "USCGC " (US Coast Guard Cutter), "R/V" (research vessel), "F/V" (fishing
# vessel), "M/V" (motor vessel) are conventional and broadly reliable. Bare-word
# heuristics like "PILOT" are kept narrower (whole-word) to avoid false positives.
_NAME_RULES: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"\bUSCGC\b", re.I), "military"),
    (re.compile(r"\bUSCG\b", re.I), "military"),
    (re.compile(r"\bUSNS\b", re.I), "military"),
    (re.compile(r"\bUSS\b", re.I), "military"),
    (re.compile(r"\bR/?V\b", re.I), "research"),
    (re.compile(r"\bRESEARCH\b", re.I), "research"),
    (re.compile(r"\bSURVEY\b", re.I), "research"),
    (re.compile(r"\bF/?V\b", re.I), "fishing"),
    (re.compile(r"\bFISHING\b", re.I), "fishing"),
    (re.compile(r"\bTRAWLER\b", re.I), "fishing"),
    (re.compile(r"\bSEINER\b", re.I), "fishing"),
    (re.compile(r"\bLONGLINER\b", re.I), "fishing"),
    (re.compile(r"\bPILOT\b", re.I), "service"),
    (re.compile(r"\bTUG\b|\bTRACTOR\b", re.I), "service"),
    (re.compile(r"\bFERRY\b", re.I), "passenger"),
    (re.compile(r"\bCRUISE\b", re.I), "passenger"),
    (re.compile(r"\bTANKER\b", re.I), "tanker"),
    (re.compile(r"\bCARGO\b|\bCONTAINER\b|\bFREIGHT\b", re.I), "cargo"),
    (re.compile(r"\bYACHT\b|\bSAILING\b", re.I), "pleasure"),
]


def classify_by_name(name: Optional[str]) -> Optional[str]:
    """Return the best vessel_type bucket for a vessel name, or None if no rule matches."""
    if not name:
        return None
    s = str(name).strip()
    if not s:
        return None
    for pattern, bucket in _NAME_RULES:
        if pattern.search(s):
            return bucket
    return None


def load_overrides(csv_path: Path) -> Dict[str, Tuple[str, Optional[str]]]:
    """Load CSV overrides as {mmsi: (vessel_type, note)}. Skips comment / blank lines."""
    out: Dict[str, Tuple[str, Optional[str]]] = {}
    if not csv_path.exists():
        return out
    with csv_path.open() as f:
        reader = csv.reader(f)
        for row in reader:
            if not row:
                continue
            first = row[0].strip()
            if not first or first.startswith("#"):
                continue
            if first.lower() == "mmsi":
                continue  # header
            if len(row) < 2:
                continue
            mmsi = first
            vessel_type = row[1].strip()
            note = row[2].strip() if len(row) >= 3 else None
            if not vessel_type:
                continue
            out[mmsi] = (vessel_type, note)
    return out


def _should_replace(current_source: Optional[str], new_source: str) -> bool:
    """Higher-priority source replaces lower; tie or unknown current => replace."""
    if current_source is None:
        return True
    return _SOURCE_PRIORITY.get(new_source, 0) >= _SOURCE_PRIORITY.get(current_source, 0)


def enrich(conn, *, overrides_path: Path, dry_run: bool = False) -> Dict[str, int]:
    overrides = load_overrides(overrides_path)
    stats = {"csv": 0, "name": 0, "skipped_existing_ais": 0}

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT mmsi, name, vessel_type, bucket_source
            FROM vessels
            ORDER BY mmsi
            """
        )
        rows = cur.fetchall()

    updates: List[Tuple[str, str, str, str]] = []  # (mmsi, vessel_type, source, note)
    for mmsi, name, current_type, current_src in rows:
        # CSV override is the highest non-AIS source
        if mmsi in overrides:
            new_type, note = overrides[mmsi]
            if _should_replace(current_src, "csv_override"):
                updates.append((mmsi, new_type, "csv_override", note or ""))
                stats["csv"] += 1
                continue
            else:
                stats["skipped_existing_ais"] += 1
                continue

        # Name-based fallback only when current is empty / weaker
        guessed = classify_by_name(name)
        if guessed and (current_type is None or current_type == "" or current_type == "unknown"):
            if _should_replace(current_src, "name_regex"):
                updates.append((mmsi, guessed, "name_regex", ""))
                stats["name"] += 1

    print(
        f"Enrichment plan: csv={stats['csv']} name={stats['name']} "
        f"skipped_existing_ais={stats['skipped_existing_ais']} (dry_run={dry_run})"
    )
    if dry_run or not updates:
        return stats

    with conn.cursor() as cur:
        for mmsi, vt, src, note in updates:
            cur.execute(
                """
                UPDATE vessels
                SET vessel_type = %s,
                    bucket_source = %s
                WHERE mmsi = %s
                """,
                (vt, src, mmsi),
            )
    conn.commit()
    return stats


def main(argv: Optional[Iterable[str]] = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Print the plan, don't write")
    parser.add_argument(
        "--overrides",
        default=str(Path(__file__).resolve().parent / "vessel_type_overrides.csv"),
        help="Path to vessel_type_overrides.csv (default: alongside this script)",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    conn = psycopg2.connect(DATABASE_URL)
    try:
        enrich(conn, overrides_path=Path(args.overrides), dry_run=args.dry_run)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
