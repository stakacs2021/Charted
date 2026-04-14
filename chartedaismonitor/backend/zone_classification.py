from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class BracketClassification:
    bracket_class: str
    bracket_source: str  # "designation" | "exception" | "unknown"


# Start empty per current requirements; populate later as you discover edge cases.
# Supported keys (checked in order):
# - zone_id: exact zone id override
# - (name, designation): exact tuple override
EXCEPTIONS_BY_ZONE_ID: dict[int, str] = {}
EXCEPTIONS_BY_NAME_AND_DESIGNATION: dict[tuple[str, str], str] = {}


# Conservative defaults: keep the number of classes small and stable.
# You can refine once you have concrete desired brackets.
_DESIGNATION_TO_BRACKET: dict[str, str] = {
    # Common California MPA designations (best-effort normalization)
    "SMR": "NoTake",
    "SMCA": "LimitedTake",
    "SMP": "SpecialClosure",
    "SMCA (NO TAKE)": "NoTake",
    "SMCA (NO-TAKE)": "NoTake",
    "SMCA NO TAKE": "NoTake",
    "SMCA NO-TAKE": "NoTake",
}


def _norm(s: Optional[str]) -> str:
    return (s or "").strip().upper()


def classify_bracket(*, designation: Optional[str], zone_id: int, name: str) -> BracketClassification:
    """
    Derive a stable bracket classification for map/UI grouping.

    - Primary input: `zones.designation` (string) with normalization.
    - Overrides: explicit exceptions (initially empty).
    """
    if zone_id in EXCEPTIONS_BY_ZONE_ID:
        return BracketClassification(EXCEPTIONS_BY_ZONE_ID[zone_id], "exception")

    d = _norm(designation)
    n = (name or "").strip()

    if n and d:
        key = (n, d)
        if key in EXCEPTIONS_BY_NAME_AND_DESIGNATION:
            return BracketClassification(EXCEPTIONS_BY_NAME_AND_DESIGNATION[key], "exception")

    if d in _DESIGNATION_TO_BRACKET:
        return BracketClassification(_DESIGNATION_TO_BRACKET[d], "designation")

    if d:
        # Fallback: keep unknown designations stable but explicit.
        return BracketClassification(f"Other:{d}", "unknown")

    return BracketClassification("Unknown", "unknown")

