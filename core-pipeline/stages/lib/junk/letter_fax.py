"""Letter / Fax junk — cover letter and fax sheets."""

from __future__ import annotations

import re

from kw import JUNK

_PATTERNS = [re.compile(p, re.IGNORECASE) for p in JUNK["letter_fax"]]


def detect_letter_fax_page(text: str) -> bool:
    if not text:
        return False
    return any(p.search(text) for p in _PATTERNS)
