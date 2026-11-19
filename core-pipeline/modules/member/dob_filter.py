"""Post-processing of the extracted DOBs, across the chart's pages (2026-11-19, user).

A member's date of birth is rarely after 2000, so a recent date is more likely a visit,
order or print date read as the DOB. Only dates with a year after ``RECENT_AFTER_YEAR``
are ever dropped:

  1. the manifest has a DOB and the page's year is more than ``MANIFEST_YEARS`` years from
     it -> dropped;
  2. otherwise, the page's date is not the chart's majority DOB (the one date most pages
     read, strictly more than any other, on at least 2 pages) -> dropped.

The member's own DOB (the manifest's) is never dropped. A dropped DOB reads Not Found
(``N/A``), so it can no longer back a name in verify_page. No date is logged.
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Optional

from ..extraction.util.dates import canonical_date
from .rules.base_rules import is_present
from .rules.field_match import dob_matches

RECENT_AFTER_YEAR = 2000
MANIFEST_YEARS = 10

DROPPED_FAR = f"dropped: after {RECENT_AFTER_YEAR}, over {MANIFEST_YEARS} years from the manifest DOB"
DROPPED_MINORITY = f"dropped: after {RECENT_AFTER_YEAR}, not the chart's majority DOB"

_ISO = re.compile(r"\d{4}-\d{2}-\d{2}")
_YEAR = re.compile(r"(?<!\d)(\d{4})(?!\d)")


def _key(value: str) -> str:
    """The date as YYYY-MM-DD when it reads as one, else the text itself (for counting)."""
    iso = canonical_date(value or "")
    return iso if _ISO.fullmatch(iso or "") else " ".join((value or "").split()).casefold()


def _year(value: str) -> Optional[int]:
    iso = canonical_date(value or "")
    if _ISO.fullmatch(iso or ""):
        return int(iso[:4])
    years = [int(y) for y in _YEAR.findall(value or "") if 1800 <= int(y) <= 2999]
    return years[0] if years else None


def drop_reasons(dobs: list[str], manifest_dob: str) -> list[Optional[str]]:
    """Per page (same order as ``dobs``): None to keep the DOB, else why it is dropped."""
    manifest_year = _year(manifest_dob) if is_present(manifest_dob) else None
    out: list[Optional[str]] = [None] * len(dobs)
    for i, dob in enumerate(dobs):
        year = _year(dob) if is_present(dob) else None
        if year is None or year <= RECENT_AFTER_YEAR or dob_matches(dob, manifest_dob):
            continue
        if manifest_year is not None and abs(year - manifest_year) > MANIFEST_YEARS:
            out[i] = DROPPED_FAR
    # The majority among the DOBs rule 1 kept.
    counts = Counter(_key(d) for d, why in zip(dobs, out) if is_present(d) and why is None)
    ranked = counts.most_common(2)
    majority = None
    if ranked and ranked[0][1] >= 2 and (len(ranked) == 1 or ranked[0][1] > ranked[1][1]):
        majority = ranked[0][0]
    if majority is None:
        return out
    for i, dob in enumerate(dobs):
        if out[i] is not None or not is_present(dob) or dob_matches(dob, manifest_dob):
            continue
        year = _year(dob)
        if year is not None and year > RECENT_AFTER_YEAR and _key(dob) != majority:
            out[i] = DROPPED_MINORITY
    return out
