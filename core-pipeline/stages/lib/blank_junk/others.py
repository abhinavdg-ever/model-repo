"""Others junk — TOC, short non-signature pages, and OCR gibberish."""

from __future__ import annotations

import re

from kw import rules
from text_utils import is_gibberish_ocr, is_signature_page, word_count

_MAX_WORDS = 20


def detect_others_page(text: str) -> bool:
    """TOC, short non-signature pages (< 20 words), or mostly gibberish OCR."""
    if not text or not text.strip():
        return False
    kw = rules()
    if any(p.search(text) for p in kw.others_toc):
        return True
    if kw.others_emr_toc_title.search(text) and any(
        m.search(text) for m in kw.others_emr_toc_markers
    ):
        return True
    if word_count(text) < _MAX_WORDS and not is_signature_page(text):
        return True
    if is_gibberish_ocr(text):
        return True
    return False


def others_reason(text: str) -> str:
    """Specific reason code for Others (for CSV ``reason`` column)."""
    kw = rules()
    if any(p.search(text or "") for p in kw.others_toc):
        return "others_toc"
    if kw.others_emr_toc_title.search(text or "") and any(
        m.search(text or "") for m in kw.others_emr_toc_markers
    ):
        return "others_emr_toc"
    if word_count(text) < _MAX_WORDS and not is_signature_page(text):
        return "others_short"
    if is_gibberish_ocr(text):
        return "others_gibberish"
    return "others"
