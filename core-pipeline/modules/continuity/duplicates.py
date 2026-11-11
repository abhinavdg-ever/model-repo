"""The date-of-service block on duplicates, once continuity knows the documents.

2026-11-11 (user: Duplicate_Logic.md, block 1). The blank / junk stage finds duplicates on their
text (``modules/blank_junk/dup_rules.py``); the documents and their Final DOS exist only here. A
pair is never a duplicate when both are Progress Note pages and their documents have different
dates of service (the document's Final DOS, not dates on the page). The copy has no page type of
its own (the stages after blank / junk skip duplicates): its text is its original's, so its page
type is the original's.

The copy's document DOS: the dates kv_extract read on the copied pages around it (its run of
duplicate and blank pages), when there are any -- a copied note carries its own; otherwise the
Final DOS of the document it sits in: the Main page before it, else the one after.

A blocked duplicate is a Main page again: "Copy of N" when its text is a 100% match
(``CONTENT_MARK``), a plain Main page otherwise (``CARRIED_MARK``). Both reasons carry
``DOS_NOTE`` and the duplicate reason they replaced, so the next continuity run checks the page
again and restores it when the dates no longer differ.

(Replaces the 2026-10-29 encounter-against-encounter re-check, removed 2026-11-11, user.)
"""
from __future__ import annotations

from typing import Any, Callable, Optional, Sequence

# The reason a copy blocked by the date of service carries when its text is a 100% match.
CONTENT_MARK = "duplicate_content_of_page_id:"
# ... and when it is not (a Rule A / Rule B duplicate): a plain Main page.
CARRIED_MARK = "carried_forward_of_page_id:"
DOS_NOTE = "blocked by date of service"
WAS = "; was: "
PROGRESS_NOTE = "Progress Note"


def _dates(row: dict[str, Any]) -> frozenset[str]:
    return frozenset(str(d) for d in (row.get("final_dos_from"), row.get("final_dos_to")) if d)


def dos_block(
    pages: Sequence[Any],
    rows: Sequence[dict[str, Any]],
    extracted_dos: Optional[dict[int, frozenset[str]]] = None,
) -> dict[int, tuple[int, frozenset[str], frozenset[str]]]:
    """Duplicates the date-of-service block stops: copy page id -> (original page id, the copy's
    document dates, the original's). ``pages`` are continuity's PageInputs in page order and
    ``rows`` its assign() rows for them."""
    dos = extracted_dos or {}
    index = {page.page_id: i for i, page in enumerate(pages)}
    out: dict[int, tuple[int, frozenset[str], frozenset[str]]] = {}
    for i, page in enumerate(pages):
        if page.kind != "duplicate" or page.duplicate_of not in index:
            continue
        o = index[page.duplicate_of]
        if pages[o].page_type != PROGRESS_NOTE:
            continue
        theirs = _dates(rows[o])
        if not theirs:
            continue
        mine = _copy_dates(pages, rows, dos, i)
        if mine and mine != theirs:
            out[page.page_id] = (page.duplicate_of, mine, theirs)
    return out


def _copy_dates(pages: Sequence[Any], rows: Sequence[dict[str, Any]],
                dos: dict[int, frozenset[str]], i: int) -> frozenset[str]:
    def copy_like(k: int) -> bool:
        return pages[k].kind in ("duplicate", "blank")

    start = i
    while start > 0 and copy_like(start - 1):
        start -= 1
    end = i
    while end + 1 < len(pages) and copy_like(end + 1):
        end += 1
    run = frozenset().union(*(dos.get(pages[k].page_id) or frozenset() for k in range(start, end + 1)))
    if run:
        return run
    for k in [*range(start - 1, -1, -1), *range(end + 1, len(pages))]:
        if pages[k].kind == "main" and rows[k].get("segment_kind") == "main":
            return _dates(rows[k])
    return frozenset()


def blocked_reason(original: int, exact: bool, mine: frozenset[str], theirs: frozenset[str],
                   was: str) -> str:
    dates = f"{', '.join(sorted(mine))} vs {', '.join(sorted(theirs))}"
    if exact:
        return f"{CONTENT_MARK}{original} (Copy of: 100% text match, {DOS_NOTE}: {dates}{WAS}{was})"
    return f"{CARRIED_MARK}{original} ({DOS_NOTE}: {dates}{WAS}{was})"


def restored_reason(reason: str, original: int) -> str:
    """The duplicate reason a blocked page had before."""
    if WAS in reason:
        was = reason.split(WAS, 1)[1]
        return was[:-1] if was.endswith(")") else was
    return f"duplicate_of_page_id:{original} (re-checked: the dates of service no longer differ)"


def exact_text(texts: dict[int, str]) -> Callable[[int, int], bool]:
    """Whether two pages' bodies are a 100% match (dup_rules: header / footer lines out)."""
    import sys

    from common.config import CORE_ROOT

    lib = str(CORE_ROOT / "modules" / "blank_junk")
    if lib not in sys.path:
        sys.path.insert(0, lib)
    import dup_rules  # noqa: E402

    cfg = dup_rules.settings()
    repeated = dup_rules.repeated_band_lines(list(texts.values()), cfg)

    def same(a: int, b: int) -> bool:
        ka = dup_rules.body(dup_rules.body_text(texts.get(a) or "", repeated, cfg)).exact_key
        kb = dup_rules.body(dup_rules.body_text(texts.get(b) or "", repeated, cfg)).exact_key
        return bool(ka) and ka == kb

    return same
