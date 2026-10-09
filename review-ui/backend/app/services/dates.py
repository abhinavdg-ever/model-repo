"""Show DOS, DOB, and signature dates as YYYY-MM-DD.

The pipeline and the extractors store several printed forms. The review
screen uses this one form for all of them.
"""

from __future__ import annotations

import re
from datetime import date, datetime

_MISSING = frozenset({"unknown", "null", "none", "n/a", "na"})

_MONTH = (
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
)
_DATE_BODY = (
    r"\d{4}-\d{2}-\d{2}"
    r"|\d{1,2}[/-]\d{1,2}[/-]\d{2,4}"
    rf"|\d{{1,2}}\.{_MONTH}\.\d{{2,4}}"
    rf"|{_MONTH}\.?\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{2,4}}"
    rf"|\d{{1,2}}(?:st|nd|rd|th)?\s+{_MONTH}\.?,?\s+\d{{2,4}}"
    rf"|\d{{1,2}}[/-]{_MONTH}[/-]\d{{2,4}}"
    rf"|{_MONTH}[/-]\d{{1,2}}[/-]\d{{2,4}}"
)
_DATE = re.compile(rf"\b({_DATE_BODY})\b", flags=re.IGNORECASE)

_FORMATS = (
    "%Y-%m-%d",
    "%m/%d/%Y",
    "%m/%d/%y",
    "%m-%d-%Y",
    "%m-%d-%y",
    "%B %d, %Y",
    "%b %d, %Y",
    "%B %d %Y",
    "%b %d %Y",
    "%b. %d, %Y",
    "%d %B %Y",
    "%d %b %Y",
    "%d.%b.%Y",
    "%d-%b-%Y",
    "%d/%b/%Y",
)


def to_iso_date(raw: object) -> str | None:
    """YYYY-MM-DD when ``raw`` is a date, else None."""
    if isinstance(raw, datetime):
        return raw.date().isoformat()
    if isinstance(raw, date):
        return raw.isoformat()
    text = re.sub(r"(\d)(?:st|nd|rd|th)\b", r"\1", str(raw or "").strip(), flags=re.IGNORECASE)
    text = re.sub(r"\bSept\b", "Sep", text, flags=re.IGNORECASE)
    text = re.sub(r"[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?$", "", text).strip().rstrip(".,;")
    if not text:
        return None
    for fmt in _FORMATS:
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def show_date(raw: object) -> str | None:
    """A single date for the screen. Missing values are None; other text is kept."""
    if raw is None:
        return None
    if isinstance(raw, (datetime, date)):
        return to_iso_date(raw)
    text = str(raw).strip()
    if not text or text.lower() in _MISSING:
        return None
    return to_iso_date(text) or text


def show_dates_in(text: str) -> str:
    """Replace every date inside a longer string. Names and other words stay."""

    def _one(match: re.Match[str]) -> str:
        return to_iso_date(match.group(1)) or match.group(0)

    return _DATE.sub(_one, text or "")
