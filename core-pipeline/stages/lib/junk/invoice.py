"""Invoice keyword junk detection — ported from document-processing junk/invoice.py."""

from __future__ import annotations

import re

INVOICE_KEYWORDS = [
    "invoice",
    "bill to",
    "ship to",
    "unit price",
    "amount due",
    "payment due",
    "remittance",
    "statement of account",
    "total due",
    "billing statement",
    "superbill",
    "charge description",
    "balance due",
    "payments received",
    "revenue reconciliation",
    "revenue reconcilation",  # common misspelling
    "revenue recon",
]

_PATTERNS = [re.compile(rf"\b{re.escape(kw.lower())}\b") for kw in INVOICE_KEYWORDS]


def detect_invoice_page(text: str) -> bool:
    if not text:
        return False
    text_lower = text.lower()
    return any(pattern.search(text_lower) is not None for pattern in _PATTERNS)
