"""Duplicates checked encounter against encounter, once continuity knows the documents.

2026-10-29 / 11-03 (user). The blank / junk stage marks a page a duplicate on its own words
(pass 1: an exact copy within 3 pages; pass 2: >= 98%, contained or < 8 words different, lengths
within 10%). A copy of a Progress Note, Operative Note, Procedure Note, Discharge Summary or Clinical
Summary page is then checked encounter against encounter:

* **The same encounter** (the copy within ``NEAR_PAGES`` (3) pages of its original, in the same
  document, or both pages with the same extracted DOS; 2026-11-03, user): a 100% match (the
  same distinct words) stays a duplicate; anything less (95-99.9%) is a **Partial**.
* **Other page types stay duplicates**: only an original whose family is
  ``ENCOUNTER_FAMILIES`` is checked.
* **Otherwise the two encounters are compared.** The copy's encounter is the document it
  sits in by position (the Main pages before and after it in one document, with the
  copies among them); a copy between documents is the run of copies, partial copies and
  blank pages around it. The anchor is the original's document. Each page of the copy's
  encounter scores its best distinct-word overlap against the anchor's pages (a page
  whose words are all on one scores 1.0), and the scores are averaged.
* **Average 98% or more -- or average 95% or more with at least half its pages an exact
  match (score 1.0; 2026-11-06, user) -- and no date differs** (the dates kv_extract read on
  the two encounters' pages): the whole encounter is a duplicate -- its duplicates stay and
  its partial duplicates (95-98%) of anchor pages become duplicates.
* **Otherwise** each copied page is judged on its own:
  - **the same distinct words (100%)**: a Main page again, "Repeated Content (to Pg X)": its
    blank / junk row keeps ``duplicate_of_page_id`` and its reason starts
    ``duplicate_content_of_page_id:<id>`` -- the later page repeats the earlier one;
  - **not a 100% match** (carried-forward content): a plain Main page, "No": no
    ``duplicate_of_page_id``, its reason starts ``carried_forward_of_page_id:<id>``.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

from config import CORE_ROOT

_JUNK_LIB = CORE_ROOT / "stages" / "lib" / "blank_junk"
if str(_JUNK_LIB) not in sys.path:
    sys.path.insert(0, str(_JUNK_LIB))

from classify import DUPLICATE_SIMILARITY_THRESHOLD, copy_runs, word_overlap  # noqa: E402

# A copy this close to its original is in the same encounter.
NEAR_PAGES = 3
# The page families (L1) whose copies are checked encounter against encounter.
# 2026-11-02 (user): Operative Note and Clinical Summary too.
# 2026-11-03 (user): Progress, Operative, Procedure Notes, Discharge and Clinical Summaries
# (Demographics is not one).
ENCOUNTER_FAMILIES = frozenset({"Progress Note", "Operative Note", "Procedure Note", "Discharge Summary", "Clinical Summary"})
# The reason a copied page carries when its encounter is not a copy.
CONTENT_MARK = "duplicate_content_of_page_id:"
# The reason a copied page carries when it is carried-forward content, not a 100% copy.
CARRIED_MARK = "carried_forward_of_page_id:"
# Duplicate content is a 100% match (2026-11-02, user).
CONTENT_MIN_OVERLAP = 1.0
# 2026-11-06 (user): an encounter averaging this much is a copy too when at least
# EXACT_PAGES_MIN_SHARE of its pages match an anchor page exactly (score 1.0).
ENCOUNTER_MIN_AVERAGE_WITH_EXACT = 0.95
EXACT_PAGES_MIN_SHARE = 0.5


@dataclass
class Review:
    # page id -> (original page id, encounter average): partial duplicates now duplicates.
    promote: dict[int, tuple[int, float]] = field(default_factory=dict)
    # page id -> (original page id, encounter average): duplicates that are Main pages
    # again (duplicate content).
    demote: dict[int, tuple[int, float]] = field(default_factory=dict)
    # page id -> (original page id, similarity): same-encounter duplicates that are not a 100%
    # match, Partial (a Main page naming the page it nearly copies; 2026-11-03, user).
    partial: dict[int, tuple[int, float]] = field(default_factory=dict)
    # page id -> (original page id, encounter average): duplicates that are plain Main pages
    # again (a single page, not a 100% match: carried-forward content).
    release: dict[int, tuple[int, float]] = field(default_factory=dict)
    # page id -> (original page id, encounter average) for every duplicate checked.
    checked: dict[int, tuple[int, float]] = field(default_factory=dict)


def _similarity(a: str, b: str) -> float:
    words = word_overlap(a, b)
    return 1.0 if words.contained >= 1.0 else words.overlap


def review(
    pages: Sequence[Any],
    rows: Sequence[dict[str, Any]],
    texts: dict[int, str],
    partial_of: dict[int, int],
    extracted_dos: Optional[dict[int, frozenset[str]]] = None,
) -> Review:
    """``pages`` are continuity's PageInputs in page order and ``rows`` its assign()
    rows for them; ``texts`` every page's own text (duplicates included); ``partial_of``
    the partial duplicates (page id -> the page it nearly copies)."""
    out = Review()
    dos = extracted_dos or {}
    index = {page.page_id: i for i, page in enumerate(pages)}
    document = [
        row.get("document_seq") if row.get("segment_kind") == "main" else None for row in rows
    ]

    def main(i: int) -> bool:
        return pages[i].kind == "main" and pages[i].page_id not in partial_of

    def copy_like(i: int) -> bool:
        return pages[i].kind in ("duplicate", "blank") or pages[i].page_id in partial_of

    def encounter_of(i: int) -> tuple[Any, list[int]]:
        """The copy's encounter: (a key, its page indices)."""
        before = next((k for k in range(i - 1, -1, -1) if main(k)), None)
        after = next((k for k in range(i + 1, len(pages)) if main(k)), None)
        if before is not None and after is not None and document[before] is not None \
                and document[before] == document[after]:
            doc = document[before]
            inside = [k for k in range(len(pages)) if main(k) and document[k] == doc]
            first, last = inside[0], inside[-1]
            return ("document", doc), sorted(
                set(inside) | {k for k in range(first, last + 1) if copy_like(k)}
            )
        start = i
        while start > 0 and copy_like(start - 1):
            start -= 1
        end = i
        while end + 1 < len(pages) and copy_like(end + 1):
            end += 1
        return ("run", start), list(range(start, end + 1))

    def anchor_of(o: int, mine: set[int]) -> list[int]:
        doc = document[o]
        if doc is None:
            return [o]
        return [k for k in range(len(pages)) if k not in mine and (k == o or (main(k) and document[k] == doc))] or [o]

    averages: dict[tuple[Any, Any], float] = {}
    exact_shares: dict[tuple[Any, Any], float] = {}
    # 2026-11-06 (user): a copy run (consecutive pages copying the pages the same distance
    # before them) of the same encounter is judged as one: every page a duplicate, or every
    # page a Partial -- never a mix page by page.
    target = {page.page_id: page.duplicate_of for page in pages if page.kind == "duplicate" and page.duplicate_of}
    target.update(partial_of)
    run_of: dict[int, int] = {}
    runs = copy_runs([page.page_id for page in pages], target)
    for n, run in enumerate(runs):
        for pid, _ in run:
            run_of[pid] = n
    run_copied: dict[int, bool] = {}

    def _run_is_copy(n: int) -> bool:
        if n not in run_copied:
            scores = [_similarity(texts.get(pid) or "", texts.get(orig) or "") for pid, orig in runs[n]]
            average = sum(scores) / len(scores)
            exact = sum(1 for score in scores if score >= 1.0) / len(scores)
            run_copied[n] = average >= DUPLICATE_SIMILARITY_THRESHOLD or (
                average >= ENCOUNTER_MIN_AVERAGE_WITH_EXACT and exact >= EXACT_PAGES_MIN_SHARE)
        return run_copied[n]

    for i, page in enumerate(pages):
        if page.kind != "duplicate":
            continue
        o = index.get(page.duplicate_of)
        if o is None:
            continue
        original = pages[o]
        if original.page_type not in ENCOUNTER_FAMILIES:
            continue
        mine, theirs = dos.get(page.page_id), dos.get(original.page_id)
        same_document = document[i] is not None and document[i] == document[o]
        if abs(i - o) <= NEAR_PAGES or same_document or (mine and theirs and mine == theirs):
            overlap = word_overlap(texts.get(page.page_id) or "", texts.get(original.page_id) or "").overlap
            run = run_of.get(page.page_id)
            if run is not None:
                if _run_is_copy(run):
                    # The whole run is a duplicate: its partials become duplicates too.
                    for pid, orig in runs[run]:
                        if pid in partial_of:
                            out.promote[pid] = (orig, overlap)
                else:
                    out.partial[page.page_id] = (original.page_id, overlap)
                continue
            # The same encounter: a 100% match stays a duplicate, anything less is a Partial.
            if overlap < CONTENT_MIN_OVERLAP:
                out.partial[page.page_id] = (original.page_id, overlap)
            continue
        key, encounter = encounter_of(i)
        members = set(encounter)
        anchor = anchor_of(o, members)
        pair = (key, document[o] if document[o] is not None else ("page", o))
        if pair not in averages:
            scores = [
                max(_similarity(texts.get(pages[k].page_id) or "", texts.get(pages[a].page_id) or "")
                    for a in anchor)
                for k in encounter
                if pages[k].kind != "blank"
            ]
            averages[pair] = sum(scores) / len(scores) if scores else 0.0
            exact_shares[pair] = sum(1 for score in scores if score >= 1.0) / len(scores) if scores else 0.0
        average = averages[pair]
        copied = average >= DUPLICATE_SIMILARITY_THRESHOLD or (
            average >= ENCOUNTER_MIN_AVERAGE_WITH_EXACT and exact_shares[pair] >= EXACT_PAGES_MIN_SHARE)
        anchor_ids = {pages[a].page_id for a in anchor}
        out.checked[page.page_id] = (original.page_id, average)
        # 2026-11-03 (user): a copied encounter also has the same dates as its original.
        copy_dates = frozenset().union(*(dos.get(pages[k].page_id) or frozenset() for k in encounter))
        anchor_dates = frozenset().union(*(dos.get(pages[a].page_id) or frozenset() for a in anchor))
        same_dates = not (copy_dates and anchor_dates and copy_dates != anchor_dates)
        if copied and same_dates:
            for k in encounter:
                pid = pages[k].page_id
                if partial_of.get(pid) in anchor_ids:
                    out.promote[pid] = (partial_of[pid], average)
        else:
            for k in encounter:
                if pages[k].kind != "duplicate" or pages[k].duplicate_of not in anchor_ids:
                    continue
                pid, orig = pages[k].page_id, pages[k].duplicate_of
                exact = word_overlap(texts.get(pid) or "", texts.get(orig) or "").overlap >= CONTENT_MIN_OVERLAP
                (out.demote if exact else out.release)[pid] = (orig, average)
    # A page demoted or released by one check is never promoted by another.
    for pid in [*out.demote, *out.release, *out.partial]:
        out.promote.pop(pid, None)
    return out
