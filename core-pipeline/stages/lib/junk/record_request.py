"""Record request / transmittal junk detection."""

from __future__ import annotations

import re

from kw import JUNK

RECORD_REQUEST_PHRASES = list(JUNK["record_request"])
_PHRASE_PATTERNS = [
    re.compile(rf"\b{re.escape(kw.lower())}\b") for kw in RECORD_REQUEST_PHRASES
]
_RECORDS_REQUEST_ANY_ORDER = re.compile(
    r"\brecords?\b.{0,40}\brequests?\b|\brequests?\b.{0,40}\brecords?\b",
    re.IGNORECASE | re.DOTALL,
)


def detect_record_request_page(text: str) -> bool:
    if not text:
        return False
    text_lower = text.lower()
    if any(p.search(text_lower) for p in _PHRASE_PATTERNS):
        return True
    return bool(_RECORDS_REQUEST_ANY_ORDER.search(text))
