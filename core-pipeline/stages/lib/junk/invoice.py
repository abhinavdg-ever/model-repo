"""Invoice keyword junk detection — ported from document-processing junk/invoice.py."""

from __future__ import annotations

import re

from kw import JUNK

INVOICE_KEYWORDS = list(JUNK["invoice"])
_PATTERNS = [re.compile(rf"\b{re.escape(kw.lower())}\b") for kw in INVOICE_KEYWORDS]
# One of these alone is enough; weaker phrases need a second hit.
_STRONG = frozenset(
    {
        "invoice",
        "superbill",
        "billing statement",
        "statement of account",
        "remittance",
        "amount due",
        "balance due",
        "total due",
        "payment due",
    }
)


def detect_invoice_page(text: str) -> bool:
    if not text:
        return False
    text_lower = text.lower()
    hits = [kw for kw, p in zip(INVOICE_KEYWORDS, _PATTERNS) if p.search(text_lower)]
    if not hits:
        return False
    if any(h.lower() in _STRONG for h in hits):
        return True
    return len(hits) >= 2
