"""Duplicate UI / CSV label + display confidence from junk flag + similarity.

2026-10-22 (user): ≥ 98% alike (or contained) is a duplicate, "Yes"; 95-98% is a
partial duplicate, "Partial", which the pipeline keeps as a Main page.
"""

from __future__ import annotations

DUPLICATE_PARTIAL_MIN = 0.95
DUPLICATE_YES_MIN = 0.98


def duplicate_display_confidence(
    is_duplicate: bool | None,
    similarity: float | None,
    is_partial: bool | None = None,
) -> float | None:
    """Map stored similarity to UI confidence.

    Yes / No → 1.0. Partial → ``1 + (sim − 1) × 10``
    (95% → 50%, 97% → 70%).
    """
    if is_duplicate is None:
        return None
    if not is_duplicate and not is_partial:
        return 1.0
    if similarity is None:
        return None
    try:
        sim = float(similarity)
    except (TypeError, ValueError):
        return None
    if sim >= DUPLICATE_YES_MIN:
        return 1.0
    return 1 + (sim - 1) * 10


def format_duplicate_label(
    is_duplicate: bool | None,
    confidence: float | None,
    is_partial: bool | None = None,
) -> str:
    """Yes for a duplicate (≥ 98%); Partial for a partial duplicate (95-98%); No otherwise."""
    del confidence  # the pipeline's flag decides; kept for callers' signature
    if is_duplicate is None:
        return "NA"
    if is_duplicate:
        return "Yes"
    if is_partial:
        return "Partial"
    return "No"
