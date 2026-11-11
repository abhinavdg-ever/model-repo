"""Duplicate UI / CSV label + display confidence from junk flag + similarity.

2026-10-22 (user): ≥ 98% alike (or contained) is a duplicate, "Yes"; 95-98% is a
partial duplicate, "Partial", which keeps its own verdict. The confidence follows the
page's best similarity (similarity_confidence).
"""

from __future__ import annotations

DUPLICATE_PARTIAL_MIN = 0.95
DUPLICATE_YES_MIN = 0.98


# Confidence from the page's best duplicate similarity (2026-10-22, user): falls
# exponentially from 100% at 100% alike to 50% at 95% (the cut between a duplicate and
# not); below it the confidence that the page is NOT a duplicate rises back to 100% at
# 80% alike and stays there.
DUPLICATE_SURE_BELOW = 0.80


def similarity_confidence(similarity: float) -> float:
    """Confidence in the duplicate call a similarity in [0, 1] makes: is a duplicate
    (Yes / Partial) at or above 95%, is not below it."""
    if similarity >= DUPLICATE_PARTIAL_MIN:
        return 0.5 ** ((1.0 - similarity) / (1.0 - DUPLICATE_PARTIAL_MIN))
    if similarity <= DUPLICATE_SURE_BELOW:
        return 1.0
    return 0.5 ** ((similarity - DUPLICATE_SURE_BELOW) / (DUPLICATE_PARTIAL_MIN - DUPLICATE_SURE_BELOW))


def format_duplicate_label(
    is_duplicate: bool | None,
    confidence: float | None,
    is_partial: bool | None = None,
    is_content: bool | None = None,
) -> str:
    """Duplicate for a duplicate; Copy for a 100% text match a hard block kept from being one
    (2026-11-11, user: Duplicate_Logic.md; Partial is gone); No otherwise."""
    del confidence, is_partial  # the pipeline's flag decides; kept for callers' signature
    if is_duplicate is None:
        return "NA"
    if is_duplicate:
        return "Duplicate"
    if is_content:
        return "Copy"
    return "No"
