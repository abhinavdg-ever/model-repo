"""Letter / Fax junk — cover letter and fax sheets."""

from __future__ import annotations

import re

_PATTERNS = [
    re.compile(r"\bcover\s+letter\b", re.IGNORECASE),
    re.compile(r"\bfax\s+transmission\b", re.IGNORECASE),
    re.compile(r"\bfax\s+cover\b", re.IGNORECASE),
    re.compile(r"\bfacsimile\b", re.IGNORECASE),
    re.compile(r"\bfacimile\b", re.IGNORECASE),  # common OCR typo
    re.compile(r"\bfax\s+sheet\b", re.IGNORECASE),
    re.compile(r"\bthis\s+fax\b", re.IGNORECASE),
    # from document-processing / sequencing cover heuristics
    re.compile(r"\bconfidentiality(\s+notice)?\b", re.IGNORECASE),
    re.compile(r"\badvantmed\b", re.IGNORECASE),
]


def detect_letter_fax_page(text: str) -> bool:
    if not text:
        return False
    return any(p.search(text) for p in _PATTERNS)
