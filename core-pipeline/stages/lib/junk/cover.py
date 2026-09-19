"""Cover page junk — short Accept / Unaccept / Cover / Discharge Summary pages."""

from __future__ import annotations

import re

from kw import JUNK
from text_utils import word_count

_COVER_PHRASES = [re.compile(p, re.IGNORECASE) for p in JUNK["cover_phrases"]]
_SINGLE_WORD_COVER = frozenset(JUNK["cover_single_words"])
_MAX_WORDS = 20


def detect_cover_page(text: str) -> bool:
    """Accept / Unaccept / Cover Page(s) / Discharge Summary with < 20 words."""
    if not text or not text.strip():
        return False
    if word_count(text) >= _MAX_WORDS:
        return False

    cleaned = re.sub(r"[^\w\s]", "", text.lower()).strip()
    words = cleaned.split()
    if len(words) == 1 and words[0] in _SINGLE_WORD_COVER:
        return True
    if "accept" in words or "unaccept" in words:
        return True
    return any(p.search(text) for p in _COVER_PHRASES)
