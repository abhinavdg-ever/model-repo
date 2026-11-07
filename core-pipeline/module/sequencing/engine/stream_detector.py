"""Stream detection — split combined uploads into document streams."""
from __future__ import annotations

import hashlib
import logging
from collections import defaultdict

from .types import ExplicitMarker, PageFeatures, Stream, StreamSplit

logger = logging.getLogger(__name__)

_MIN_FINGERPRINT_TEXT_LENGTH = 30


def detect_streams(
    features: list[PageFeatures],
    markers: dict[str, ExplicitMarker],
) -> StreamSplit:
    classified = sorted([f for f in features if f.is_classified], key=lambda f: f.original_page_number)
    sequenceable = [f for f in features if not f.is_classified]
    sequenceable_ids = {f.page_id for f in sequenceable}

    complete_markers = {
        pid: m for pid, m in markers.items() if m.page_num is not None and pid in sequenceable_ids
    }
    if not complete_markers:
        return StreamSplit(streams=[], orphans=list(sequenceable), classified=classified, duplicate_page_ids=set())

    feature_map = {f.page_id: f for f in sequenceable}
    by_total: dict[int, list[PageFeatures]] = defaultdict(list)
    for page_id, marker in complete_markers.items():
        feature = feature_map.get(page_id)
        if feature:
            by_total[marker.total_pages or 0].append(feature)

    if by_total:
        largest_group_size = max(len(g) for g in by_total.values())
        if largest_group_size >= 3:
            for total_pages, group in list(by_total.items()):
                if len(group) <= 2 and len(group) < largest_group_size / 3:
                    for feature in group:
                        complete_markers.pop(feature.page_id, None)
                    del by_total[total_pages]

    if 0 < len(complete_markers) <= 2 and len(sequenceable) >= 5:
        complete_markers.clear()
        by_total.clear()

    if not complete_markers:
        return StreamSplit(streams=[], orphans=list(sequenceable), classified=classified, duplicate_page_ids=set())

    streams: list[Stream] = []
    duplicate_page_ids: set[str] = set()
    stream_index = 0
    for total_pages in sorted(by_total.keys()):
        group = sorted(by_total[total_pages], key=lambda f: f.original_page_number)
        group_streams, group_dups = _split_group_into_streams(group, complete_markers, total_pages, stream_index)
        streams.extend(group_streams)
        duplicate_page_ids.update(group_dups)
        stream_index += len(group_streams)

    for stream in streams:
        stream.features = [f for f in stream.features if f.page_id not in duplicate_page_ids]
        stream.markers = {pid: m for pid, m in stream.markers.items() if pid not in duplicate_page_ids}
    streams = [s for s in streams if s.features]

    assigned = set()
    for stream in streams:
        assigned.update(f.page_id for f in stream.features)
    assigned.update(duplicate_page_ids)
    orphans = [f for f in sequenceable if f.page_id not in assigned]

    streams.sort(key=lambda s: min(f.original_page_number for f in s.features))
    for i, stream in enumerate(streams):
        stream.stream_id = f"split_{i + 1:03d}"

    if len(streams) == 1 and orphans:
        streams[0].features.extend(orphans)
        orphans = []

    logger.info(
        "Stream detection: streams=%d orphans=%d classified=%d duplicates=%d total=%d",
        len(streams),
        len(orphans),
        len(classified),
        len(duplicate_page_ids),
        len(features),
    )
    return StreamSplit(
        streams=streams,
        orphans=orphans,
        classified=classified,
        duplicate_page_ids=duplicate_page_ids,
    )


def _split_group_into_streams(
    group_features: list[PageFeatures],
    markers: dict[str, ExplicitMarker],
    total_pages: int,
    stream_index_start: int,
) -> tuple[list[Stream], set[str]]:
    if not group_features:
        return [], set()

    duplicate_page_ids: set[str] = set()
    by_page_num: dict[int, list[PageFeatures]] = defaultdict(list)
    for feature in group_features:
        by_page_num[markers[feature.page_id].page_num].append(feature)

    conflict_streams: list[list[PageFeatures]] = []
    for _page_num, candidates in sorted(by_page_num.items()):
        kept, dups = _deduplicate_by_content(candidates)
        duplicate_page_ids.update(d.page_id for d in dups)

        if not conflict_streams:
            for page in kept:
                conflict_streams.append([page])
            continue

        if len(kept) > 1 and len(conflict_streams) > 1:
            assigned_streams: set[int] = set()
            assigned_pages: set[str] = set()
            edges = []
            for page in kept:
                for i, stream in enumerate(conflict_streams):
                    if stream:
                        dist = abs(stream[-1].original_page_number - page.original_page_number)
                        edges.append((dist, page, i))
            edges.sort(key=lambda x: x[0])
            for _dist, page, stream_idx in edges:
                if page.page_id not in assigned_pages and stream_idx not in assigned_streams:
                    conflict_streams[stream_idx].append(page)
                    assigned_streams.add(stream_idx)
                    assigned_pages.add(page.page_id)
            for page in kept:
                if page.page_id not in assigned_pages:
                    for j in range(max(len(conflict_streams), len(kept))):
                        if j not in assigned_streams:
                            if j >= len(conflict_streams):
                                conflict_streams.append([])
                            conflict_streams[j].append(page)
                            assigned_streams.add(j)
                            break
        else:
            for i, page in enumerate(kept):
                if i >= len(conflict_streams):
                    conflict_streams.append([])
                conflict_streams[i].append(page)

    streams: list[Stream] = []
    stream_idx = stream_index_start
    for i, stream_features in enumerate(conflict_streams):
        if not stream_features:
            continue
        stream_features.sort(key=lambda f: f.original_page_number)
        stream_idx += 1
        streams.append(
            Stream(
                stream_id=f"split_{stream_idx:03d}",
                total_pages=total_pages,
                features=stream_features,
                markers={f.page_id: markers[f.page_id] for f in stream_features},
                is_conflict=(i > 0),
            )
        )
    return streams, duplicate_page_ids


def _deduplicate_by_content(candidates: list[PageFeatures]) -> tuple[list[PageFeatures], list[PageFeatures]]:
    if len(candidates) <= 1:
        return candidates, []

    ordered = sorted(candidates, key=lambda f: f.original_page_number)
    fingerprints: dict[str, list[PageFeatures]] = defaultdict(list)
    no_text: list[PageFeatures] = []
    for feature in ordered:
        if feature.text_fingerprint:
            fingerprints[feature.text_fingerprint].append(feature)
            continue
        text = feature.full_text or ""
        if len(text) < _MIN_FINGERPRINT_TEXT_LENGTH:
            no_text.append(feature)
            continue
        fingerprints[_text_fingerprint(text)].append(feature)

    kept: list[PageFeatures] = list(no_text)
    duplicates: list[PageFeatures] = []
    for group in fingerprints.values():
        kept.append(group[0])
        duplicates.extend(group[1:])
    return kept, duplicates


def _text_fingerprint(text: str) -> str:
    return hashlib.sha256(" ".join(text.lower().split()).encode("utf-8")).hexdigest()
