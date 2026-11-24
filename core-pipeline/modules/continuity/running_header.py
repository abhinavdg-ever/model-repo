"""The running header / footer of an EHR print (2026-11-24, user: a CCD print of four visits, each
page headed 'Patient Name ... Encounter Id: N  Encounter Date: <date>' and numbered '1', '2', '3'
at the foot, came back as the wrong documents).

Hard rules on two content pages next to each other, in both continuity modes (the engine applies
them after the clue model's decision):

1. Both headers carry a labelled encounter / visit id and they differ -> a new document.
2. Else both headers carry a labelled encounter / visit date and they differ -> a new document.
3. A bare page number in the footer band (a line that is only "2" or "Page 2", no total) that
   restarts at 1 -> a new document; one that follows (n, then n + 1) -> the same document, unless
   the earlier page is signed (a signed page ends its document, 2026-11-01).

Only lines in the header / footer bands count, and bare numbers only on pages with line positions
(a medication quantity on the last line of a page without them reads like a page number).
"""
from __future__ import annotations

import re
from typing import Iterable, Optional

_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), start=1)}

# 'Encounter Id: 2033825', 'Visit #: A1234', 'CSN: 123456789', 'FIN No. 77812' -- an id label is
# required, so 'Encounter Date' is never read as an id.
_ID = re.compile(
    r"(?i)\b(?:encounter|visit|enc|csn|fin|account|acct)\s*(?:#|no\.?|number|num|id)\s*[:#]?\s*"
    r"([a-z0-9][a-z0-9\-]{3,})\b"
)
_CSN = re.compile(r"(?i)\b(?:csn|fin)\s*[:#]\s*([a-z0-9][a-z0-9\-]{3,})\b")
_DATE_LABEL = r"(?:encounter|visit|service)\s*date|date\s*of\s*(?:service|visit)|\bDOS\b"
_NUMERIC = re.compile(r"(?i)(?:" + _DATE_LABEL + r")\s*[:\-]?\s*(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2,4})")
_WORDED = re.compile(r"(?i)(?:" + _DATE_LABEL + r")\s*[:\-]?\s*([a-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})")
_BARE_PAGE = re.compile(r"(?i)^\s*(?:page|pg\.?)?\s*(\d{1,3})\s*$")


def header_ids(lines: Iterable[str]) -> frozenset[str]:
    """Labelled encounter / visit ids printed in the header lines (with a digit in them)."""
    found: set[str] = set()
    for line in lines:
        for pattern in (_ID, _CSN):
            for m in pattern.finditer(line or ""):
                value = m.group(1).casefold()
                if any(c.isdigit() for c in value):
                    found.add(value)
    return frozenset(found)


def header_date(lines: Iterable[str]) -> Optional[str]:
    """The labelled encounter / visit date in the header lines, as YYYY-MM-DD; None if none."""
    for line in lines:
        m = _NUMERIC.search(line or "")
        if m:
            mo, d, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
            y = y + 2000 if y < 100 else y
            if 1 <= mo <= 12 and 1 <= d <= 31:
                return f"{y:04d}-{mo:02d}-{d:02d}"
        m = _WORDED.search(line or "")
        if m:
            mo = _MONTHS.get(m.group(1)[:3].casefold())
            d, y = int(m.group(2)), int(m.group(3))
            if mo and 1 <= d <= 31:
                return f"{y:04d}-{mo:02d}-{d:02d}"
    return None


def footer_number(lines: Iterable[str]) -> Optional[int]:
    """A bare page number in the footer lines ('2', 'Page 2'; no total), the lowest line's."""
    number = None
    for line in lines:
        m = _BARE_PAGE.match(line or "")
        if m and 1 <= int(m.group(1)) <= 200:
            number = int(m.group(1))
    return number


def decision(head_a: list[str], head_b: list[str], foot_a: Optional[list[str]], foot_b: Optional[list[str]],
             a_signed_ends: bool = False) -> Optional[tuple[bool, str, str]]:
    """(break, rule, why) for the meeting point of two content pages, or None. ``foot_*`` None:
    the page has no line positions (bare numbers not read). ``a_signed_ends``: the earlier page is
    signed and that ends its document (no page-number continue over it)."""
    ids_a, ids_b = header_ids(head_a), header_ids(head_b)
    if ids_a and ids_b and not ids_a & ids_b:
        return True, "header_id_change", "running header: the encounter / visit id changes"
    if not (ids_a and ids_b):
        date_a, date_b = header_date(head_a), header_date(head_b)
        if date_a and date_b and date_a != date_b:
            return True, "header_date_change", "running header: the encounter / visit date changes"
    if foot_a is None or foot_b is None:
        return None
    na, nb = footer_number(foot_a), footer_number(foot_b)
    if na is None or nb is None:
        return None
    if nb == 1:
        return True, "page_number_restart", f"footer page number restarts ({na} -> 1)"
    if nb == na + 1 and not a_signed_ends:
        return False, "page_number_follows", f"footer page number follows ({na} -> {nb})"
    return None
