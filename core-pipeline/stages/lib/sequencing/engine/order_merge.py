"""Merge marker anchors, cross-encoder chains, and scan order into one document sequence."""
from __future__ import annotations

from .types import ExplicitMarker, PageFeatures, SequenceAssignment


def unified_page_order(
    features: list[PageFeatures],
    markers: dict[str, ExplicitMarker],
    assignments: dict[str, SequenceAssignment],
    ce_chain: list[str],
    conflicts: set[str],
) -> list[str]:
    """Global page order from explicit markers + original scan order.

    Marker numbers and scan positions are different scales (a partial upload of
    a 160-page record numbers 2..90; per-note print counters restart), so raw
    marker numbers cannot be sorted against original page numbers directly.
    Instead, marker pages are reordered **within the slots they already
    occupy**: the set of scan positions held by marker pages stays fixed, and
    the marker pages are placed into those slots by ascending marker number.
    Unmarked pages keep their scan position. Degrades to pure original order
    when there are no markers.
    """
    if not features:
        return []

    ordered = sorted(features, key=lambda f: f.original_page_number)
    marked = [f for f in ordered if f.page_id in markers and f.page_id not in conflicts]
    if len(marked) < 2:
        return [f.page_id for f in ordered]

    marked_ids = {f.page_id for f in marked}
    slots = [i for i, f in enumerate(ordered) if f.page_id in marked_ids]
    by_marker = sorted(
        marked, key=lambda f: (int(markers[f.page_id].page_num), f.original_page_number)
    )
    result: list[PageFeatures] = list(ordered)
    for slot, feature in zip(slots, by_marker):
        result[slot] = feature
    return [f.page_id for f in result]
