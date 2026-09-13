"""Others junk — TOC, short non-signature pages, and OCR gibberish."""

from __future__ import annotations

import re

from text_utils import is_gibberish_ocr, is_signature_page, word_count

_TOC_PATTERNS = [
    re.compile(r"\btable\s+of\s+contents?\b", re.IGNORECASE),
    re.compile(r"\bcontents?\s+page\b", re.IGNORECASE),
]

# EMR export TOC: "Patient Medical Record" + (total pages OR "N to M")
_EMR_TOC_TITLE = re.compile(r"\bpatient\s+medical\s+record\b", re.IGNORECASE)
_EMR_TOC_MARKERS = (
    re.compile(r"\btotal\s+pages\b", re.IGNORECASE),
    re.compile(r"\b\d{1,4}\s+to\s+\d{1,4}\b", re.IGNORECASE),
)

_MAX_WORDS = 20


def detect_others_page(text: str) -> bool:
    """TOC, short non-signature pages (< 20 words), or mostly gibberish OCR."""
    if not text or not text.strip():
        return False
    if any(p.search(text) for p in _TOC_PATTERNS):
        return True
    # document-processing style EMR TOC (title + structure marker)
    if _EMR_TOC_TITLE.search(text) and any(m.search(text) for m in _EMR_TOC_MARKERS):
        return True
    if word_count(text) < _MAX_WORDS and not is_signature_page(text):
        return True
    if is_gibberish_ocr(text):
        return True
    return False


def others_reason(text: str) -> str:
    """Specific reason code for Others (for CSV ``reason`` column)."""
    if any(p.search(text or "") for p in _TOC_PATTERNS):
        return "others_toc"
    if _EMR_TOC_TITLE.search(text or "") and any(
        m.search(text or "") for m in _EMR_TOC_MARKERS
    ):
        return "others_emr_toc"
    if word_count(text) < _MAX_WORDS and not is_signature_page(text):
        return "others_short"
    if is_gibberish_ocr(text):
        return "others_gibberish"
    return "others"
