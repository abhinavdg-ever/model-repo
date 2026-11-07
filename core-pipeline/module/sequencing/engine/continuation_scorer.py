"""Cross-encoder continuation matching for conflicting pages."""
from __future__ import annotations

import re

from .. import cross_encoder
from ..config import HIGH_CONFIDENCE_THRESHOLD, MAX_PAIR_SCORES_PER_JOB
from .types import PageFeatures

_CONTINUATION_RE = re.compile(r"\bcont(?:inued)?\.?\b", re.IGNORECASE)


def build_transition_scores(features: list[PageFeatures]) -> dict[tuple[str, str], float]:
    if len(features) < 2:
        return {}
    pairs: list[tuple[str, str]] = []
    pair_keys: list[tuple[str, str]] = []
    for left in features:
        for right in features:
            if left.page_id == right.page_id:
                continue
            # Limit to nearby pages in scan order — full N² blows the pair budget on real jobs.
            if abs(left.original_page_number - right.original_page_number) > 12:
                continue
            pair_keys.append((left.page_id, right.page_id))
            pairs.append((left.bottom_lines_text or "", right.top_lines_text or ""))

    if not pairs or len(pairs) > MAX_PAIR_SCORES_PER_JOB:
        return {}

    scores_list = cross_encoder.score_pairs(pairs)
    return dict(zip(pair_keys, scores_list))


def build_context_chain(
    features: list[PageFeatures],
    unresolved_page_ids: set[str],
) -> tuple[list[str], dict[str, float]]:
    candidates = [f for f in features if f.page_id in unresolved_page_ids]
    if not candidates:
        return [], {}
    if len(candidates) == 1:
        return [candidates[0].page_id], {}

    if not cross_encoder.available():
        ordered = sorted(candidates, key=lambda f: f.original_page_number)
        return [f.page_id for f in ordered], {}

    scores = build_transition_scores(candidates)
    if not scores:
        ordered = sorted(candidates, key=lambda f: f.original_page_number)
        return [f.page_id for f in ordered], {}

    feature_map = {f.page_id: f for f in candidates}

    remaining = {f.page_id for f in candidates}
    start = _find_start_page(candidates, scores)
    chain = [start]
    remaining.discard(start)
    edge_scores: dict[str, float] = {}

    current = start
    while remaining:
        nxt = max(
            remaining,
            key=lambda pid: (
                scores.get((current, pid), 0.0),
                -abs(feature_map[pid].original_page_number - feature_map[current].original_page_number),
            ),
        )
        edge_scores[nxt] = scores.get((current, nxt), 0.0)
        chain.append(nxt)
        remaining.discard(nxt)
        current = nxt
    return chain, edge_scores


def _find_start_page(features: list[PageFeatures], scores: dict[tuple[str, str], float]) -> str:
    def effective_incoming(feature: PageFeatures) -> tuple[float, int]:
        max_incoming = max(
            (scores.get((other.page_id, feature.page_id), 0.0) for other in features if other.page_id != feature.page_id),
            default=0.0,
        )
        is_continuation = bool(_CONTINUATION_RE.search(feature.header_text or "")) or bool(
            _CONTINUATION_RE.search(feature.top_lines_text or "")
        )
        return max_incoming + (1.0 if is_continuation else 0.0), feature.original_page_number

    return min(features, key=effective_incoming).page_id


__all__ = ["HIGH_CONFIDENCE_THRESHOLD", "build_context_chain", "build_transition_scores"]
