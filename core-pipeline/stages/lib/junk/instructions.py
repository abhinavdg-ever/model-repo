"""Instructions junk — pages telling what to send / provide."""

from __future__ import annotations

import re

_PATTERNS = [
    re.compile(r"\bwhat\s+to\s+send\b", re.IGNORECASE),
    re.compile(r"\bprovide\s+all\s+documentation\b", re.IGNORECASE),
    re.compile(r"\bprovide\s+documentation\b", re.IGNORECASE),
    re.compile(r"\binstructions?\s+for\s+(sending|submission|records)\b", re.IGNORECASE),
    re.compile(r"\bplease\s+send\b", re.IGNORECASE),
    re.compile(r"\bdocuments?\s+to\s+(include|send|attach)\b", re.IGNORECASE),
]


def detect_instructions_page(text: str) -> bool:
    if not text:
        return False
    return any(p.search(text) for p in _PATTERNS)
