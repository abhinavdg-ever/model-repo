"""Detect-only sequencing (suggested order; does not reorder files on disk).

Ported from advantmed-document-processing ``adapters/sequencing/detect.py``.
"""
from __future__ import annotations

import logging
from typing import Any

from .engine.cover_order import reorder_cover_pages_last
from .engine.marker_detector import detect_markers
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
    Optional: is_classified (blank/junk/duplicate → excluded from main order).
    """
    if not pages:
        return []

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

    assignments = reorder_cover_pages_last(assignments, features)
    for position, assignment in enumerate(
        sorted(assignments, key=lambda a: a.sequence_position or a.original_page_number),
        start=1,
    ):
        assignment.sequence_position = position

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
