"""Unit tests for the name-regex classifier and CSV loader (no DB)."""
from __future__ import annotations

import textwrap
from pathlib import Path

from scripts.enrich_vessel_types import classify_by_name, load_overrides


def test_classify_by_name_handles_common_prefixes():
    assert classify_by_name("USCGC Stratton") == "military"
    assert classify_by_name("R/V Reuben Lasker") == "research"
    assert classify_by_name("F/V Pacific Glory") == "fishing"
    assert classify_by_name("Pilot Boat San Francisco") == "service"
    assert classify_by_name("M/V Catalina Express") is None  # M/V alone is not enough
    assert classify_by_name("Catalina Express FERRY") == "passenger"
    assert classify_by_name("Some Tanker") == "tanker"
    assert classify_by_name("Container Vessel ABC") == "cargo"
    assert classify_by_name("Yacht Wanderlust") == "pleasure"
    assert classify_by_name("Unknown Boat") is None
    assert classify_by_name("") is None
    assert classify_by_name(None) is None


def test_classify_is_case_insensitive_and_word_bounded():
    # Word boundary -> 'tug' inside 'struggle' should NOT match
    assert classify_by_name("struggle") is None
    assert classify_by_name("tug McTugFace") == "service"
    assert classify_by_name("USCG Auxiliary") == "military"


def test_load_overrides_skips_comments_and_blanks(tmp_path: Path):
    csv_content = textwrap.dedent(
        """\
        mmsi,vessel_type,note
        # this is a comment
        366970320,research,NOAA R/V

        369970201,military,USCGC Stratton
        ,,empty row
        """
    )
    p = tmp_path / "overrides.csv"
    p.write_text(csv_content)

    out = load_overrides(p)
    assert out == {
        "366970320": ("research", "NOAA R/V"),
        "369970201": ("military", "USCGC Stratton"),
    }


def test_load_overrides_missing_file_returns_empty(tmp_path: Path):
    assert load_overrides(tmp_path / "nope.csv") == {}
