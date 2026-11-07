"""Header/footer fallback grouping via Jaccard token overlap."""
from __future__ import annotations

from .types import PageFeatures

HEADER_SIMILARITY_THRESHOLD = 0.70
IDENTITY_SIMILARITY_THRESHOLD = 0.60


def build_header_groups(features: list[PageFeatures], unresolved_page_ids: set[str]) -> dict[str, str]:
    candidates = [
        f
        for f in features
        if f.page_id in unresolved_page_ids and (f.header_text or f.footer_text or f.identity_text)
    ]
    parent = {f.page_id: f.page_id for f in candidates}

    for index, left in enumerate(candidates):
        for right in candidates[index + 1 :]:
            sim = max(
                jaccard_similarity(left.header_text, right.header_text),
                jaccard_similarity(left.footer_text, right.footer_text),
            )
            identity_sim = jaccard_similarity(left.identity_text, right.identity_text)
            if sim > HEADER_SIMILARITY_THRESHOLD or identity_sim > IDENTITY_SIMILARITY_THRESHOLD:
                _union(parent, left.page_id, right.page_id)

    clusters: dict[str, list[str]] = {}
    for page_id in parent:
        clusters.setdefault(_find(parent, page_id), []).append(page_id)

    group_map: dict[str, str] = {}
    group_index = 1
    for page_ids in sorted(clusters.values(), key=lambda values: min(values)):
        if len(page_ids) < 2:
            continue
        group_id = f"header_{group_index:03d}"
        group_index += 1
        for page_id in page_ids:
            group_map[page_id] = group_id
    return group_map


def jaccard_similarity(left: str, right: str) -> float:
    left_words = set((left or "").split())
    right_words = set((right or "").split())
    if not left_words or not right_words:
        return 0.0
    return len(left_words & right_words) / len(left_words | right_words)


def _find(parent: dict[str, str], page_id: str) -> str:
    while parent[page_id] != page_id:
        parent[page_id] = parent[parent[page_id]]
        page_id = parent[page_id]
    return page_id


def _union(parent: dict[str, str], left: str, right: str) -> None:
    left_root = _find(parent, left)
    right_root = _find(parent, right)
    if left_root != right_root:
        parent[right_root] = left_root
