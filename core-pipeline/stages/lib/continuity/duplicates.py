"""Duplicates checked encounter against encounter, once continuity knows the documents.

2026-10-29 (user). The blank / junk stage marks a page a duplicate on its own words. A
Progress Note or Discharge Summary page that is a copy of a page far away is only a
duplicate when its whole encounter is a copy of its original's:

* **The same encounter stays a duplicate**: the copy within ``NEAR_PAGES`` (3) pages of
  its original, or both pages with the same extracted (kv_extract) DOS.
* **Other page types stay duplicates**: only an original whose family is
  ``ENCOUNTER_FAMILIES`` is checked.
* **Otherwise the two encounters are compared.** The copy's encounter is the document it
  sits in by position (the Main pages before and after it in one document, with the
  copies among them); a copy between documents is the run of copies, partial copies and
  blank pages around it. The anchor is the original's document. Each page of the copy's
  encounter scores its best distinct-word overlap against the anchor's pages (a page
  whose words are all on one scores 1.0), and the scores are averaged.
* **Average above 98%**: the whole encounter is a duplicate -- its duplicates stay and
  its partial duplicates (95-98%) of anchor pages become duplicates. A page that is
  neither stays as it is.
* **98% or less**: only the page was copied. Each duplicate of an anchor page in the
  encounter is a Main page again, "No (Duplicate Content)": its blank / junk row keeps
  ``duplicate_of_page_id`` and its reason starts ``duplicate_content_of_page_id:<id>``.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

from config import CORE_ROOT

_JUNK_LIB = CORE_ROOT / "stages" / "lib" / "blank_junk"
if str(_JUNK_LIB) not in sys.path:
    sys.path.insert(0, str(_JUNK_LIB))

from classify import DUPLICATE_SIMILARITY_THRESHOLD, word_overlap  # noqa: E402

# A copy this close to its original is in the same encounter.
NEAR_PAGES = 3
# The page families (L1) whose copies are checked encounter against encounter.
ENCOUNTER_FAMILIES = frozenset({"Progress Note", "Discharge Summary"})
# The reason a copied page carries when its encounter is not a copy.
CONTENT_MARK = "duplicate_content_of_page_id:"


@dataclass
class Review:
    # page id -> (original page id, encounter average): partial duplicates now duplicates.
    promote: dict[int, tuple[int, float]] = field(default_factory=dict)
    # page id -> (original page id, encounter average): duplicates that are Main pages
    # again (duplicate content).
    demote: dict[int, tuple[int, float]] = field(default_factory=dict)
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
    for i, page in enumerate(pages):
        if page.kind != "duplicate":
            continue
        o = index.get(page.duplicate_of)
        if o is None:
            continue
        original = pages[o]
        if abs(i - o) <= NEAR_PAGES:
            continue  # the same encounter
        mine, theirs = dos.get(page.page_id), dos.get(original.page_id)
        if mine and theirs and mine == theirs:
            continue  # the same DOS: the same encounter
        if original.page_type not in ENCOUNTER_FAMILIES:
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
        average = averages[pair]
        anchor_ids = {pages[a].page_id for a in anchor}
        out.checked[page.page_id] = (original.page_id, average)
        if average > DUPLICATE_SIMILARITY_THRESHOLD:
            for k in encounter:
                pid = pages[k].page_id
                if partial_of.get(pid) in anchor_ids:
                    out.promote[pid] = (partial_of[pid], average)
        else:
            for k in encounter:
                if pages[k].kind == "duplicate" and pages[k].duplicate_of in anchor_ids:
                    out.demote[pages[k].page_id] = (pages[k].duplicate_of, average)
    # A page demoted by one check is never promoted by another.
    for pid in out.demote:
        out.promote.pop(pid, None)
    return out
