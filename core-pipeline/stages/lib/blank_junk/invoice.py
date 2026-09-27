"""Invoice keyword junk detection — ported from document-processing junk/invoice.py."""

from __future__ import annotations

import re

from kw import rules

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
    kw = rules()
    hits = [
        k for k, p in zip(kw.invoice_keywords, kw.invoice_patterns) if p.search(text_lower)
    ]
    if not hits:
        return False
    if any(h.lower() in _STRONG for h in hits):
        return True
    return len(hits) >= 2
