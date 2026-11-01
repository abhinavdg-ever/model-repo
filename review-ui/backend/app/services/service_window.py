"""The requested date-of-service window, read from the manifest's service note (2026-11-01, user).

The client's service note says which dates the chart must cover, per measure:

    TRC - REQUIRED - Member demographic sheet ... 2026 Hospital discharge summaries, 2026 ...
    BCS - REQUIRED - Include each and every DOS for Oct 2024 to Dec 2026. ...

``service_window(note, measure)`` gives (from, to, the words it was read from):

* when the note has a section per measure ("TRC - ...", "BCS - ...") and the chart's
  measure is one of them, only that section is read;
* a written range comes first: "Oct 2024 to Dec 2026", "October 2024 - December 2026",
  "01/01/2026 - 12/31/2026", "1/1/2026 through 12/31/2026" (a month's range runs from its
  first day to its last);
* else the years the section names on their own ("2026 Hospital discharge summaries"):
  1 January of the first to 31 December of the last;
* nothing found: no window (the Service Date tab says the note gives none).

"through 12/31/2026" on its own (evidence of a mastectomy any time through ...) is not a
window: it says how far back evidence may go, not which visits are asked for.
"""
from __future__ import annotations

import calendar
import re
from datetime import date
from typing import Optional

_MONTHS = {name: i for i, name in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
_MONTH = r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?"
_TO = r"\s*(?:-|–|—|to|through|thru|until)\s*"
_MONTH_RANGE = re.compile(rf"(?i)\b{_MONTH}\s+(\d{{4}}){_TO}{_MONTH}\s+(\d{{4}})\b")
_DATE = r"(\d{1,2})/(\d{1,2})/(\d{4})"
_DATE_RANGE = re.compile(rf"(?i)\b{_DATE}{_TO}{_DATE}\b")
_YEAR = re.compile(r"(?<![\d/])(20\d{2})(?![\d/])")
_SECTION = re.compile(r"(?m)^\s*([A-Z][A-Z0-9]{1,9})\s*-\s*")


def _month_no(name: str) -> int:
    return _MONTHS[name.lower()[:3]]


def section_for(note: str, measure: Optional[str]) -> str:
    """The part of the note for ``measure`` when the note has a section per measure."""
    starts = [(m.start(), m.group(1)) for m in _SECTION.finditer(note or "")]
    if not starts or not measure:
        return note or ""
    wanted = measure.strip().upper()
    for i, (at, code) in enumerate(starts):
        if code == wanted or wanted.startswith(code) or code.startswith(wanted):
            end = starts[i + 1][0] if i + 1 < len(starts) else len(note)
            return note[at:end]
    return note


def service_window(note: Optional[str], measure: Optional[str] = None) -> tuple[Optional[str], Optional[str], str]:
    """(from ISO, to ISO, the words read) for the requested DOS window; (None, None, "") when
    the note gives none."""
    text = section_for(note or "", measure)
    if not text.strip():
        return None, None, ""
    found = _MONTH_RANGE.search(text)
    if found:
        m1, y1, m2, y2 = found.groups()
        start = date(int(y1), _month_no(m1), 1)
        end_month, end_year = _month_no(m2), int(y2)
        end = date(end_year, end_month, calendar.monthrange(end_year, end_month)[1])
        if end >= start:
            return start.isoformat(), end.isoformat(), found.group(0)
    found = _DATE_RANGE.search(text)
    if found:
        mo1, d1, y1, mo2, d2, y2 = (int(x) for x in found.groups())
        try:
            start, end = date(y1, mo1, d1), date(y2, mo2, d2)
        except ValueError:
            start = end = None
        if start and end and end >= start:
            return start.isoformat(), end.isoformat(), found.group(0)
    years = sorted({int(y) for y in _YEAR.findall(text)})
    if years:
        words = str(years[0]) if len(years) == 1 else f"{years[0]}–{years[-1]}"
        return date(years[0], 1, 1).isoformat(), date(years[-1], 12, 31).isoformat(), words
    return None, None, ""
