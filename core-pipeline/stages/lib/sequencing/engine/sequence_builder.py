"""Resolve page order within one stream — sequence first, unified global merge."""
from __future__ import annotations

from .continuation_scorer import HIGH_CONFIDENCE_THRESHOLD, build_context_chain
from .header_fingerprint import build_header_groups
from .marker_detector import find_marker_conflicts
from .order_merge import unified_page_order
from .scorer import clamp_confidence
from .types import ExplicitMarker, PageFeatures, SequenceAssignment


def build_sequence(
    features: list[PageFeatures],
    markers: dict[str, ExplicitMarker],
    *,
    cross_encoder_enabled: bool = True,
    header_footer_enabled: bool = True,
) -> list[SequenceAssignment]:
    classified = sorted([f for f in features if f.is_classified], key=lambda f: f.original_page_number)
    sequenceable = sorted([f for f in features if not f.is_classified], key=lambda f: f.original_page_number)
    feature_map = {f.page_id: f for f in sequenceable}

    conflicts = find_marker_conflicts({pid: m for pid, m in markers.items() if pid in feature_map})

    assignments: dict[str, SequenceAssignment] = {}
    ce_chain: list[str] = []

    marker_ids = [
        f.page_id for f in sequenceable if f.page_id in markers and f.page_id not in conflicts
    ]
    for pid in marker_ids:
        marker = markers[pid]
        n = len(sequenceable)
        absolute = marker.total_pages == n
        method = "explicit_marker" if (marker.total_pages is not None and absolute) else "explicit_marker_reference"
        confidence = marker.confidence if absolute else min(marker.confidence, 0.90)
        assignments[pid] = _assignment(feature_map[pid], method, confidence, False, marker=marker)

    unresolved_ids = [f.page_id for f in sequenceable if f.page_id not in assignments]

    if unresolved_ids and cross_encoder_enabled and len(unresolved_ids) > 1:
        unresolved_features = [feature_map[pid] for pid in unresolved_ids]
        ce_chain, edge_scores = build_context_chain(unresolved_features, set(unresolved_ids))
        if ce_chain:
            for pid in ce_chain:
                score = edge_scores.get(pid)
                is_conflict = pid in conflicts
                if pid in assignments:
                    continue
                if score is None:
                    assignments[pid] = _assignment(
                        feature_map[pid], "cross_encoder", 0.60, is_conflict, marker=markers.get(pid)
                    )
                elif score >= HIGH_CONFIDENCE_THRESHOLD:
                    assignments[pid] = _assignment(
                        feature_map[pid],
                        "cross_encoder",
                        score,
                        is_conflict,
                        marker=markers.get(pid),
                        continuation_score=score,
                    )
                else:
                    assignments[pid] = _assignment(
                        feature_map[pid],
                        "cross_encoder",
                        max(score, 0.0),
                        True,
                        marker=markers.get(pid),
                        continuation_score=score,
                    )

    if header_footer_enabled:
        remaining = [feature_map[pid] for pid in unresolved_ids if pid not in assignments]
        if remaining:
            groups = build_header_groups(remaining, {f.page_id for f in remaining})
            for group_id in sorted(set(groups.values())):
                group_pids = sorted(
                    [pid for pid, g in groups.items() if g == group_id],
                    key=lambda pid: feature_map[pid].original_page_number,
                )
                has_marker = any(pid in markers for pid in group_pids)
                for pid in group_pids:
                    if pid in assignments:
                        continue
                    assignments[pid] = _assignment(
                        feature_map[pid],
                        "header_group",
                        0.90 if has_marker else 0.75,
                        pid in conflicts,
                        marker=markers.get(pid),
                        header_group_id=group_id,
                    )

    for f in sequenceable:
        if f.page_id not in assignments:
            assignments[f.page_id] = _assignment(
                feature_map[f.page_id], "original_order", 0.30, True, marker=markers.get(f.page_id)
            )

    ordered_ids = unified_page_order(sequenceable, markers, assignments, ce_chain, conflicts)

    result: list[SequenceAssignment] = []
    for position, pid in enumerate(ordered_ids, start=1):
        assignment = assignments[pid]
        assignment.sequence_position = position
        result.append(assignment)

    for offset, feature in enumerate(classified, start=1):
        result.append(
            SequenceAssignment(
                page_id=feature.page_id,
                original_page_number=feature.original_page_number,
                sequence_position=len(ordered_ids) + offset,
                sequence_method="classified_page",
                sequence_confidence=clamp_confidence(0.30),
                sequence_review_flag=True,
            )
        )
    return result


def _assignment(
    feature: PageFeatures,
    method: str,
    confidence: float,
    review_flag: bool,
    *,
    marker: ExplicitMarker | None = None,
    header_group_id: str | None = None,
    continuation_score: float | None = None,
) -> SequenceAssignment:
    return SequenceAssignment(
        page_id=feature.page_id,
        original_page_number=feature.original_page_number,
        sequence_position=None,
        sequence_method=method,
        sequence_confidence=clamp_confidence(confidence),
        sequence_review_flag=review_flag,
        explicit_marker_found=marker is not None,
        marker_page_num=marker.page_num if marker else None,
        marker_total_pages=marker.total_pages if marker else None,
        header_group_id=header_group_id,
        continuation_score=clamp_confidence(continuation_score) if continuation_score is not None else None,
    )
