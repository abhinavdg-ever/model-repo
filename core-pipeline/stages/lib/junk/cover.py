"""Cover page junk — short Accept / Unaccept / Cover / Discharge Summary pages."""

from __future__ import annotations

import re

from text_utils import word_count

# Short cover-style pages (word count must be < 20)
_COVER_PHRASES = [
    re.compile(r"\bcover\s+pages?\b", re.IGNORECASE),
    re.compile(r"\bdischarge\s+summary\b", re.IGNORECASE),
]
_SINGLE_WORD_COVER = frozenset({"accept", "unaccept", "summary"})
_MAX_WORDS = 20


def detect_cover_page(text: str) -> bool:
    """Accept / Unaccept / Cover Page(s) / Discharge Summary with < 20 words."""
    if not text or not text.strip():
        return False
    wc = word_count(text)
    if wc >= _MAX_WORDS:
        return False

    cleaned = re.sub(r"[^\w\s]", "", text.lower()).strip()
    words = cleaned.split()
    if len(words) == 1 and words[0] in _SINGLE_WORD_COVER:
        return True
    if "accept" in words or "unaccept" in words:
        return True
    return any(p.search(text) for p in _COVER_PHRASES)
