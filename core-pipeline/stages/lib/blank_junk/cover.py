"""Cover page junk — short Accept / Unaccept / Cover / Discharge Summary pages."""

from __future__ import annotations

import re

from kw import rules
from text_utils import word_count

_MAX_WORDS = 20


def detect_cover_page(text: str) -> bool:
    """Accept / Unaccept / Cover Page(s) / Discharge Summary with < 20 words."""
    if not text or not text.strip():
        return False
    if word_count(text) >= _MAX_WORDS:
        return False

    kw = rules()
    cleaned = re.sub(r"[^\w\s]", "", text.lower()).strip()
    words = cleaned.split()
    if len(words) == 1 and words[0] in kw.cover_single_words:
        return True
    if "accept" in words or "unaccept" in words:
        return True
    return any(p.search(text) for p in kw.cover_phrases)
