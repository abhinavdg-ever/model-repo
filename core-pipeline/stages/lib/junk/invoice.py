"""Invoice keyword junk detection — ported from document-processing junk/invoice.py."""

from __future__ import annotations

import re

from kw import JUNK

INVOICE_KEYWORDS = list(JUNK["invoice"])
_PATTERNS = [re.compile(rf"\b{re.escape(kw.lower())}\b") for kw in INVOICE_KEYWORDS]


def detect_invoice_page(text: str) -> bool:
    if not text:
        return False
    text_lower = text.lower()
    return any(p.search(text_lower) for p in _PATTERNS)
