"""Move cover pages to the end of the sequence (fax covers, transmittals)."""
from __future__ import annotations

from ..page_heuristics import is_cover_page
from .types import PageFeatures, SequenceAssignment


def reorder_cover_pages_last(
    assignments: list[SequenceAssignment],
    features: list[PageFeatures],
) -> list[SequenceAssignment]:
    feature_map = {f.page_id: f for f in features}
    main: list[SequenceAssignment] = []
    covers: list[SequenceAssignment] = []

    for assignment in sorted(assignments, key=lambda a: a.sequence_position or a.original_page_number):
        feature = feature_map.get(assignment.page_id)
        if feature and is_cover_page(
            original_page_number=feature.original_page_number,
            header_text=feature.header_text,
            footer_text=feature.footer_text,
            full_text=feature.full_text,
            word_count=feature.word_count,
        ):
            covers.append(assignment)
        else:
            main.append(assignment)

    if not covers:
        return assignments

    reordered = main + covers
    for position, assignment in enumerate(reordered, start=1):
        assignment.sequence_position = position
    return reordered
