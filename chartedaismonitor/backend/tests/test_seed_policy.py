"""Unit tests for the policy-row builder (no DB)."""
from __future__ import annotations

from scripts.seed_policy import BRACKETS, NON_FISHING_BUCKETS, build_rows


def _to_lookup(rows):
    return {(b, t): allowed for b, t, allowed, _ in rows}


def test_build_rows_default_allows_transit_everywhere():
    rows = build_rows(strict_unknown=False)
    lk = _to_lookup(rows)
    for bracket in BRACKETS:
        for bucket in NON_FISHING_BUCKETS:
            assert lk[(bracket, bucket)] is True, f"{bracket}/{bucket} should be allowed"


def test_build_rows_default_denies_fishing_in_strict_zones():
    rows = build_rows(strict_unknown=False)
    lk = _to_lookup(rows)
    assert lk[("NoTake", "fishing")] is False
    assert lk[("SpecialClosure", "fishing")] is False
    assert lk[("LimitedTake", "fishing")] is True


def test_build_rows_unknown_default_is_permissive():
    rows = build_rows(strict_unknown=False)
    lk = _to_lookup(rows)
    assert lk[("NoTake", "unknown")] is True
    assert lk[("SpecialClosure", "unknown")] is True
    assert lk[("LimitedTake", "unknown")] is True


def test_build_rows_strict_unknown_denies_in_strict_zones():
    rows = build_rows(strict_unknown=True)
    lk = _to_lookup(rows)
    assert lk[("NoTake", "unknown")] is False
    assert lk[("SpecialClosure", "unknown")] is False
    # LimitedTake stays permissive in either mode
    assert lk[("LimitedTake", "unknown")] is True


def test_build_rows_no_duplicates():
    rows = build_rows(strict_unknown=False)
    keys = [(b, t) for b, t, _, _ in rows]
    assert len(keys) == len(set(keys)), "policy rows must have unique (bracket, bucket) pairs"
