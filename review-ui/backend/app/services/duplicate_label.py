"""Duplicate UI / CSV label + display confidence from junk flag + similarity.

2026-10-22 (user): ≥ 98% alike (or contained) is a duplicate, "Yes"; 95-98% is a
partial duplicate, "Partial", which keeps its own verdict. The confidence (2026-11-14)
follows the match score: duplicate_match_confidence for Yes / Partial,
duplicate_no_confidence for No.
"""

from __future__ import annotations

DUPLICATE_PARTIAL_MIN = 0.95
DUPLICATE_YES_MIN = 0.98


# Duplicate confidence (2026-11-14, user; docs/specials/CONFIDENCE_LOGIC.md 12.2.1, the same
# numbers as core-pipeline/resources/canon/confidence_canon.json "duplicate"). Replaces the
# 2026-10-22 V-curve.
DUPLICATE_MATCH_SLOPE = 10.0
DUPLICATE_FLOOR = 0.30
DUPLICATE_SURE_BELOW = 0.80
DUPLICATE_GOOD_UNTIL = 0.90
DUPLICATE_GOOD_SLOPE = 2.0
DUPLICATE_NEAR_SLOPE = 5.0


def _clip(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def duplicate_match_confidence(similarity: float) -> float:
    """Confidence in Yes / Partial from the match score s to the original:
    max(0.30, 1 - 10 x (1 - s)): 1.0 -> 1.0, 0.98 -> 0.80, 0.95 -> 0.50."""
    return max(DUPLICATE_FLOOR, 1.0 - DUPLICATE_MATCH_SLOPE * (1.0 - _clip(similarity)))


def duplicate_no_confidence(similarity: float | None) -> float:
    """Confidence in No from the best match score s to any compared page: < 0.80 -> 1.0;
    0.80-0.90 -> 1 - 2 x (s - 0.80); above -> 0.80 - 5 x (s - 0.90), floor 0.30. Nothing
    compared (None): 1.0."""
    if similarity is None:
        return 1.0
    s = _clip(similarity)
    if s < DUPLICATE_SURE_BELOW:
        return 1.0
    if s <= DUPLICATE_GOOD_UNTIL:
        return 1.0 - DUPLICATE_GOOD_SLOPE * (s - DUPLICATE_SURE_BELOW)
    top = 1.0 - DUPLICATE_GOOD_SLOPE * (DUPLICATE_GOOD_UNTIL - DUPLICATE_SURE_BELOW)
    return max(DUPLICATE_FLOOR, top - DUPLICATE_NEAR_SLOPE * (s - DUPLICATE_GOOD_UNTIL))


def similarity_confidence(similarity: float) -> float:
    """Confidence in the call a similarity makes: Yes / Partial at or above 95% (the
    match curve), No below it (the No curve)."""
    if similarity >= DUPLICATE_PARTIAL_MIN:
        return duplicate_match_confidence(similarity)
    return duplicate_no_confidence(similarity)


def format_duplicate_label(
    is_duplicate: bool | None,
    confidence: float | None,
    is_partial: bool | None = None,
    is_content: bool | None = None,
) -> str:
    """Yes for a duplicate; Partial for a 100% text match a hard block kept from being one
    (2026-11-13, user; were Duplicate / Copy); No otherwise."""
    del confidence, is_partial  # the pipeline's flag decides; kept for callers' signature
    if is_duplicate is None:
        return "NA"
    if is_duplicate:
        return "Yes"
    if is_content:
        return "Partial"
    return "No"
