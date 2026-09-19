"""Instructions junk — pages telling what to send / provide."""

from __future__ import annotations

import re

from kw import JUNK

_PATTERNS = [re.compile(p, re.IGNORECASE) for p in JUNK["instructions"]]


def detect_instructions_page(text: str) -> bool:
    if not text:
        return False
    return any(p.search(text) for p in _PATTERNS)
