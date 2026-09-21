"""Duplicate UI / CSV label + display confidence from junk flag + similarity."""

from __future__ import annotations

DUPLICATE_MAYBE_MIN = 0.95
DUPLICATE_YES_MIN = 1.0


def duplicate_display_confidence(
    is_duplicate: bool | None,
    similarity: float | None,
) -> float | None:
    """Map stored similarity to UI confidence.

    Yes / No → 1.0. May Be → ``1 + (sim − 1) × 10``
    (98% → 80%, 99% → 90%, 95% → 50%).
    """
    if is_duplicate is None:
        return None
    if not is_duplicate:
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
) -> str:
    """Yes at 100%; May Be at [95%, 100%); No otherwise."""
    if is_duplicate is None:
        return "NA"
    if not is_duplicate:
        return "No"
    if confidence is None:
        return "May Be"
    try:
        sim = float(confidence)
    except (TypeError, ValueError):
        return "May Be"
    if sim >= DUPLICATE_YES_MIN:
        return "Yes"
    if sim >= DUPLICATE_MAYBE_MIN:
        return "May Be"
    return "No"
