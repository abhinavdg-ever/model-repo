"""Short pages whose words are all on a nearby page: Junk / Others (2026-11-01, user).

Runs in continuity, after blank / junk pass 2 and the duplicate check, when the documents
are known. A Main page is junked when:

* **it is short**: its text (whitespace stripped) is under ``SHORT_SHARE`` (20%) of the
  chart's median Main page, and it has at least ``MIN_WORDS`` distinct words;
* **it is not signed** (a signature page is kept, as blank / junk R4 keeps it);
* **every one of its distinct words is on one longer Main page** (100% contained) that is
  within ``NEAR_PAGES`` (3) pages before or after it, or anywhere in the same Progress
  Note (the document it sits in, when the document's first page is a Progress Note).

The page it is contained in is recorded in the reason: ``short_page_contained_in_page_id:<id>``.
"""
from __future__ import annotations

import statistics
import sys
from typing import Any, Optional, Sequence

from config import CORE_ROOT

_JUNK_LIB = CORE_ROOT / "stages" / "lib" / "blank_junk"
if str(_JUNK_LIB) not in sys.path:
    sys.path.insert(0, str(_JUNK_LIB))

from classify import unique_words  # noqa: E402

SHORT_SHARE = 0.20
NEAR_PAGES = 3
MIN_WORDS = 3
DOCUMENT_FAMILIES = frozenset({"Progress Note"})
MARK = "short_page_contained_in_page_id:"
CONFIDENCE = 0.90


def _length(text: str) -> int:
    return len("".join((text or "").split()))


def find(
    pages: Sequence[Any],
    rows: Sequence[dict[str, Any]],
    texts: dict[int, str],
) -> dict[int, tuple[int, str]]:
    """Page id -> (the page id it is contained in, why) for each short page to junk.
    ``pages`` are continuity's PageInputs in page order, ``rows`` its assign() rows and
    ``texts`` every page's own text."""
    main = [i for i, page in enumerate(pages) if page.kind == "main" and _length(texts.get(page.page_id) or "")]
    if not main:
        return {}
    lengths = {i: _length(texts.get(pages[i].page_id) or "") for i in main}
    median = statistics.median(lengths.values())
    limit = SHORT_SHARE * median
    words = {i: unique_words(texts.get(pages[i].page_id) or "") for i in main}

    document = [row.get("document_seq") if row.get("segment_kind") == "main" else None for row in rows]
    first_type: dict[Any, str] = {}
    for i, doc in enumerate(document):
        if doc is not None and doc not in first_type:
            first_type[doc] = pages[i].page_type or ""

    out: dict[int, tuple[int, str]] = {}
    for i in main:
        page = pages[i]
        mine = words[i]
        if lengths[i] >= limit or len(mine) < MIN_WORDS or page.signed:
            continue
        doc = document[i]
        same_note = doc is not None and first_type.get(doc) in DOCUMENT_FAMILIES
        candidates = [
            k for k in main
            if k != i and lengths[k] > lengths[i]
            and (abs(k - i) <= NEAR_PAGES or (same_note and document[k] == doc))
        ]
        container: Optional[int] = next(
            (k for k in sorted(candidates, key=lambda k: (abs(k - i), k)) if mine <= words[k]), None
        )
        if container is None:
            continue
        where = "within 3 pages" if abs(container - i) <= NEAR_PAGES else "in the same Progress Note"
        out[page.page_id] = (
            pages[container].page_id,
            f"{MARK}{pages[container].page_id} (short page: {lengths[i]} characters < "
            f"{SHORT_SHARE:.0%} of the chart's median {median:.0f}; all {len(mine)} distinct words "
            f"on page {pages[container].page_number} {where}) -> Junk / Others",
        )
    return out
