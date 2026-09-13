"""Record request / transmittal junk detection."""

from __future__ import annotations

import re

# Phrase / pattern hits (order-independent "records"+"request" handled separately)
RECORD_REQUEST_PHRASES = [
    "request letter",
    "e-request letter",
    "medical records attached",
    "records attached",
    "attached records",
    "transmittal",
    "transmitted",
    "transmittal sheet",
    "facimile transmittal sheet",
    "medical records transmittal",
    "urgent request for records",
    "your records requested",
    "records requested",
    "request for medical records",
    "medical records request",
    "medical record request",
    "risk adjustment request",
    "send record",
    "packet may contain",
    "audit fulfillment",
    # from document-processing junk/cover.py
    "intended recipient",
    "transmission contains",
    "confidential medical records",
]

_PHRASE_PATTERNS = [
    re.compile(rf"\b{re.escape(kw.lower())}\b") for kw in RECORD_REQUEST_PHRASES
]

# "records" and "request" in any order within a short window
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
