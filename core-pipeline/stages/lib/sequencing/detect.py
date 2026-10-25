"""Detect-only sequencing (suggested order; does not reorder files on disk).

Ported from advantmed-document-processing ``adapters/sequencing/detect.py``. The engine
finds each page's printed page number, its method and confidence; the order itself is
``fenced_order`` (2026-10-25, user).
"""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any, Optional

from .engine.marker_detector import _EXPLICIT_PATTERNS, detect_markers
from .engine.scorer import clamp_confidence
from .engine.sequence_builder import build_sequence
from .engine.stream_detector import detect_streams
from .engine.types import ExplicitMarker, SequenceAssignment, StreamSplit
from .feature_extractor import features_from_pages

logger = logging.getLogger(__name__)


def compute_sequence_assignments(
    pages: list[dict[str, Any]],
    *,
    cross_encoder_enabled: bool = True,
) -> list[dict[str, Any]]:
    """Return per-page suggested sequencing fields keyed by page_id (str).

    Each input page dict needs: page_id, page_number, text.
    Optional: is_classified (blank/junk/duplicate → excluded from main order), document
    (its continuity document: pages move within it, see ``fenced_order``).
    """
    if not pages:
        return []
    documents = {str(p["page_id"]): p.get("document") for p in pages}

    features = features_from_pages(pages)
    markers = detect_markers(features, doc_page_count=len(pages))
    split = detect_streams(features, markers)

    if split.is_single_stream:
        assignments = build_sequence(
            features,
            markers,
            cross_encoder_enabled=cross_encoder_enabled,
            header_footer_enabled=True,
        )
    else:
        assignments = _sequence_multi_stream(
            features,
            split,
            markers,
            cross_encoder_enabled=cross_encoder_enabled,
        )

    order, held = fenced_order(features, markers, documents)
    position_of = {pid: n for n, pid in enumerate(order, start=1)}
    for assignment in assignments:
        assignment.sequence_position = position_of.get(assignment.page_id, assignment.sequence_position)
        if assignment.page_id in held:
            assignment.sequence_method = "marker_held"
            assignment.sequence_review_flag = True

    out: list[dict[str, Any]] = []
    for a in assignments:
        out.append(
            {
                "page_id": a.page_id,
                "original_page_number": a.original_page_number,
                "sequence_position": a.sequence_position,
                "sequence_method": a.sequence_method,
                "sequence_confidence": clamp_confidence(a.sequence_confidence),
                "sequence_review_flag": bool(a.sequence_review_flag),
                "stream_id": a.stream_id or "default",
                "marker_type": a.sequence_method if a.explicit_marker_found else None,
                "marker_value": a.marker_page_num,
                "marker_total_pages": a.marker_total_pages,
                "marker_confidence": (
                    clamp_confidence(a.sequence_confidence)
                    if a.explicit_marker_found
                    else None
                ),
                "explicit_marker_found": bool(a.explicit_marker_found),
            }
        )
    return out


WINDOW = 5  # the farthest a page moves from its scan position (2026-10-25, user)


def fenced_order(features: list, markers: dict[str, ExplicitMarker], documents: dict[str, Optional[int]],
                 window: int = WINDOW) -> tuple[list[str], set[str]]:
    """(page ids in the suggested order, pages held in place).

    Main pages keep their scan order, except pages that confidently say "page n of m":
    a total in an explicit pattern ("Page 3 of 7", "Pg 3 of 7", "p. 3 of 7", "Page 3/7")
    or read so by the key/value extraction. Pages with the same m are one family and
    take the family's scan positions in page-number order; a page without a confident
    "n of m" never moves. A family may span continuity documents (2026-10-25, user);
    when its numbers repeat (two printouts of the same length) it splits back by
    document. A page moves at most ``window`` places -- unless every Main page of the
    documents it spans belongs to the family (the whole document is numbered n of m), so
    a document scanned fully in reverse is put back in order. A family is held in scan
    order, and flagged, when two of its pages claim one number or a page would move too
    far."""
    ordered = sorted(features, key=lambda f: f.original_page_number)
    index = {f.page_id: i for i, f in enumerate(ordered)}
    pages_of: dict[Any, set[str]] = defaultdict(set)
    for f in ordered:
        pages_of[documents.get(f.page_id)].add(f.page_id)
    families: dict[int, list] = defaultdict(list)
    for f in ordered:
        marker = markers.get(f.page_id)
        if marker is None or not marker.page_num or marker.total_pages is None:
            continue
        if marker.pattern not in _EXPLICIT_PATTERNS and marker.source != "extracted":
            continue
        families[marker.total_pages].append(f)
    groups: list[list] = []
    for members in families.values():
        numbers = [markers[f.page_id].page_num for f in members]
        if len(set(numbers)) < len(numbers):
            by_document: dict[Any, list] = defaultdict(list)
            for f in members:
                by_document[documents.get(f.page_id)].append(f)
            groups.extend(by_document.values())
        else:
            groups.append(members)

    result = list(ordered)
    held: set[str] = set()
    for members in groups:
        if len(members) < 2:
            continue
        ids = {f.page_id for f in members}
        whole = all(pages_of[documents.get(f.page_id)] <= ids for f in members)
        numbers = [markers[f.page_id].page_num for f in members]
        slots = sorted(index[f.page_id] for f in members)
        target = sorted(members, key=lambda f: (markers[f.page_id].page_num, f.original_page_number))
        moved = [f for slot, f in zip(slots, target) if slot != index[f.page_id]]
        too_far = not whole and any(abs(slot - index[f.page_id]) > window for slot, f in zip(slots, target))
        if len(set(numbers)) < len(numbers) or too_far:
            held.update(f.page_id for f in moved or members)
            continue
        for slot, f in zip(slots, target):
            result[slot] = f
    return [f.page_id for f in result], held


def _sequence_multi_stream(
    all_features: list,
    split: StreamSplit,
    markers: dict[str, ExplicitMarker],
    *,
    cross_encoder_enabled: bool,
) -> list[SequenceAssignment]:
    feature_map = {f.page_id: f for f in all_features}
    out: list[SequenceAssignment] = []

    for stream in split.streams:
        stream_assignments = build_sequence(
            stream.features,
            stream.markers,
            cross_encoder_enabled=cross_encoder_enabled,
            header_footer_enabled=True,
        )
        for assignment in stream_assignments:
            assignment.stream_id = stream.stream_id
            if stream.is_conflict:
                assignment.sequence_review_flag = True
        out.extend(stream_assignments)

    if split.orphans:
        orphan_markers = {
            f.page_id: markers[f.page_id] for f in split.orphans if f.page_id in markers
        }
        out.extend(
            build_sequence(
                split.orphans,
                orphan_markers,
                cross_encoder_enabled=cross_encoder_enabled,
                header_footer_enabled=True,
            )
        )

    for dup_id in split.duplicate_page_ids:
        feature = feature_map.get(dup_id)
        if feature:
            out.append(_classified_assignment(feature, "duplicate_page"))
    for feature in split.classified:
        out.append(_classified_assignment(feature, "classified_page"))

    for position, assignment in enumerate(out, start=1):
        assignment.sequence_position = position
    return out


def _classified_assignment(feature, method: str) -> SequenceAssignment:
    return SequenceAssignment(
        page_id=feature.page_id,
        original_page_number=feature.original_page_number,
        sequence_position=None,
        sequence_method=method,
        sequence_confidence=clamp_confidence(0.30),
        sequence_review_flag=True,
        stream_id="classified",
    )
