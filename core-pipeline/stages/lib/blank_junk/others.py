"""Others junk — table of contents and OCR gibberish.

Short pages are not judged here: postprocess.py junks them in pass 2 only, with the
same signature test as every other rule.
"""

from __future__ import annotations

import re

from kw import rules
from text_utils import is_gibberish_ocr


def detect_others_page(text: str) -> bool:
    """A table of contents, or mostly gibberish OCR."""
    if not text or not text.strip():
        return False
    kw = rules()
    if any(p.search(text) for p in kw.others_toc):
        return True
    if kw.others_emr_toc_title.search(text) and any(
        m.search(text) for m in kw.others_emr_toc_markers
    ):
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
    if is_gibberish_ocr(text):
        return "others_gibberish"
    return "others"
