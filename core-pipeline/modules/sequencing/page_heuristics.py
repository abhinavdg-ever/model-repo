"""Lightweight page heuristics for sequencing (cover, blank)."""
from __future__ import annotations

import re

_COVER_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r"\bcover\s*(?:page|sheet)?\b", re.I),
    re.compile(r"\bfax\s+cover\b", re.I),
    re.compile(r"\btransmittal\b", re.I),
    re.compile(r"\bconfidentiality\s+notice\b", re.I),
    re.compile(r"\bpatient\s+record\s+from\b", re.I),
    re.compile(r"\bmedical\s+record\s+from\b", re.I),
)

_BLANK_MAX_WORDS = 12
_BLANK_MAX_CHARS = 80


def matches_any(text: str, patterns: tuple[re.Pattern, ...]) -> bool:
    if not text:
        return False
    return any(p.search(text) for p in patterns)


def is_near_blank(text: str, *, word_count: int | None = None) -> bool:
    stripped = (text or "").strip()
    if not stripped:
        return True
    words = word_count if word_count is not None else len(re.findall(r"\b\w+\b", stripped))
    if words <= _BLANK_MAX_WORDS and len(stripped) <= _BLANK_MAX_CHARS:
        return True
    if words <= 3 and len(stripped) < 40:
        return True
    return False


