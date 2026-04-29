#!/usr/bin/env python3
"""
Seed `zone_vessel_type_policy` with the default California-MPA stance:

  "Transit is legal; only fishing-type vessels in strict zones (NoTake, SpecialClosure)
  generate violations. Unknown-type vessels are temporarily allowed everywhere while
  vessel-type coverage ramps up; flip via this script (or SQL) once coverage is good."

Matrix (rows = bracket, columns = vessel_type bucket):

                 cargo  tanker  passenger  service  pleasure  research  military  fishing  unknown
NoTake             A      A        A         A        A          A         A        D        A*
LimitedTake        A      A        A         A        A          A         A        A        A*
SpecialClosure     A      A        A         A        A          A         A        D        A*

A = allowed, D = denied. (* = configurable; see UNKNOWN_DEFAULTS at the bottom.)

Idempotent: re-running upserts rows without dropping anything you've added by hand.

Usage
-----
    docker compose exec backend python scripts/seed_policy.py
    docker compose exec backend python scripts/seed_policy.py --strict-unknown   # flip unknown=allow -> unknown=deny in NoTake/SpecialClosure
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

import psycopg2

_backend = Path(__file__).resolve().parent.parent
if str(_backend) not in sys.path:
    sys.path.insert(0, str(_backend))
from database import DATABASE_URL  # type: ignore


BRACKETS = ("NoTake", "LimitedTake", "SpecialClosure")
NON_FISHING_BUCKETS = ("cargo", "tanker", "passenger", "service", "pleasure", "research", "military")


def build_rows(strict_unknown: bool) -> List[Tuple[str, str, bool, str]]:
    """Return list of (bracket, vessel_type, allowed, note)."""
    rows: List[Tuple[str, str, bool, str]] = []

    # Non-fishing vessels: presence (transit) is legal in every bracket. CA MPA rules
    # govern *take*, not transit, so a cargo ship cutting a corner shouldn't be a violation.
    for bracket in BRACKETS:
        for bucket in NON_FISHING_BUCKETS:
            rows.append((bracket, bucket, True, "transit is legal under CA MPA regs"))

    # Fishing
    rows.append(("NoTake", "fishing", False, "no take of any marine resource in SMRs"))
    rows.append(("SpecialClosure", "fishing", False, "no take in special closures"))
    rows.append(("LimitedTake", "fishing", True, "limited take allowed in SMCAs (refine per-site later)"))

    # Unknown bucket: NULL vessel_type is collapsed to 'unknown' in is_vessel_allowed_in_zone.
    if strict_unknown:
        rows.append(("NoTake", "unknown", False, "strict mode: unknown type denied in SMR"))
        rows.append(("SpecialClosure", "unknown", False, "strict mode: unknown type denied in SMP"))
    else:
        rows.append(("NoTake", "unknown", True, "permissive default; tighten once type coverage is good"))
        rows.append(("SpecialClosure", "unknown", True, "permissive default; tighten once type coverage is good"))
    rows.append(("LimitedTake", "unknown", True, "presence is fine in SMCAs regardless of type"))

    return rows


def upsert(conn, rows: Iterable[Tuple[str, str, bool, str]]) -> int:
    n = 0
    with conn.cursor() as cur:
        for bracket, bucket, allowed, note in rows:
            cur.execute(
                """
                INSERT INTO zone_vessel_type_policy (zone_bracket_class, vessel_type, allowed, note)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (zone_bracket_class, vessel_type) DO UPDATE SET
                    allowed = EXCLUDED.allowed,
                    note = EXCLUDED.note;
                """,
                (bracket, bucket, allowed, note),
            )
            n += 1
    conn.commit()
    return n


def main(argv: Iterable[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--strict-unknown",
        action="store_true",
        help="Deny unknown-type vessels in NoTake/SpecialClosure (default: allow)",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    rows = build_rows(strict_unknown=args.strict_unknown)
    conn = psycopg2.connect(DATABASE_URL)
    try:
        n = upsert(conn, rows)
        print(f"zone_vessel_type_policy: upserted {n} rows (strict_unknown={args.strict_unknown})")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
