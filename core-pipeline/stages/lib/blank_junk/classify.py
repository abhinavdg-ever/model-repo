"""Junk classification codes + text rules."""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from functools import lru_cache
from typing import Optional

from blank import detect_blank_page, is_declared_blank_page
from cover import detect_cover_page
from instructions import detect_instructions_page
from invoice import detect_invoice_page
from letter_fax import detect_letter_fax_page
from others import detect_others_page, others_reason
from record_request import detect_record_request_page
from text_utils import looks_like_clinical_content

CODE_MAIN = 0
CODE_BLANK = 1
CODE_INVOICE = 2
CODE_COVER_PAGE = 3
CODE_DUPLICATE = 4
CODE_RECORD_REQUEST = 5
CODE_INSTRUCTIONS = 6
CODE_OTHERS = 7
CODE_LETTER_FAX = 8
# Junk with no sub-type: a page the model called Blank that still has text.
CODE_JUNK = 9

# Back-compat alias
CODE_COVER = CODE_COVER_PAGE

CLASSIFICATION_LABELS = {
    CODE_MAIN: "Main",
    CODE_BLANK: "Blank",
    CODE_INVOICE: "Invoice",
    CODE_COVER_PAGE: "Cover Page",
    CODE_DUPLICATE: "Duplicate",
    CODE_RECORD_REQUEST: "Record Request/Transmittal",
    CODE_INSTRUCTIONS: "Instructions",
    CODE_OTHERS: "Others",
    CODE_LETTER_FAX: "Letter/Fax",
    CODE_JUNK: "Junk",
}

JUNK_CODES = frozenset(
    {
        CODE_BLANK,
        CODE_INVOICE,
        CODE_COVER_PAGE,
        CODE_RECORD_REQUEST,
        CODE_INSTRUCTIONS,
        CODE_OTHERS,
        CODE_LETTER_FAX,
        CODE_JUNK,
    }
)

_MIN_FINGERPRINT_CHARS = 50
# 2026-10-22 (user): similarity is the average of two matchers (text_similarity).
# At or above this, or one page's text wholly inside the other's, the page is a
# duplicate: flag ``duplicate``, UI Duplicate "Yes", Blank / Junk "Yes (Duplicate)".
DUPLICATE_SIMILARITY_THRESHOLD = 0.98
# [PARTIAL, THRESHOLD) is a partial duplicate: the page stays Main (not Yes
# (Duplicate)) and records the page it nearly copies; UI Duplicate "Partial".
DUPLICATE_PARTIAL_THRESHOLD = 0.95
# Pages shorter than this (whitespace-stripped characters, ~50 words) are never
# compared: a short page shares or sits inside too much unrelated text.
DUPLICATE_MIN_CHARS = 300
DUPLICATE_NEIGHBOR_WINDOW = 25  # 2026-10-21 (user): was 10 (5 before 2026-10-20)

DEFAULT_SETTINGS = {
    "blank_detection": True,
    "invoice_detection": True,
    "cover_page_detection": True,
    "record_request_detection": True,
    "instructions_detection": True,
    "others_detection": True,
    "letter_fax_detection": True,
    "duplicate_detection": True,
    "declared_blank_detection": True,
}


def classify_text(
    full_text: str,
    *,
    settings: dict | None = None,
) -> tuple[int, str]:
    """Return (code, reason).

    Priority:
      blank → cover page → letter/fax → invoice → record request →
      instructions → others → main

    Substantive clinical notes (progress note / HPI / meds / …) short-circuit
    to Main so footer "confidentiality notice" or a stray billing phrase cannot
    mark a real chart page as junk.
    """
    cfg = {**DEFAULT_SETTINGS, **(settings or {})}

    if cfg.get("blank_detection") and detect_blank_page(full_text):
        return CODE_BLANK, "blank_ocr"
    if cfg.get("declared_blank_detection") and is_declared_blank_page(text=full_text):
        return CODE_BLANK, "declared_blank"
    # Cover is length-gated (<20 words); still allow it before the clinical guard.
    if cfg.get("cover_page_detection") and detect_cover_page(full_text):
        return CODE_COVER_PAGE, "cover_page"
    if looks_like_clinical_content(full_text):
        return CODE_MAIN, "clinical_content"
    if cfg.get("letter_fax_detection") and detect_letter_fax_page(full_text):
        return CODE_LETTER_FAX, "letter_fax"
    if cfg.get("invoice_detection") and detect_invoice_page(full_text):
        return CODE_INVOICE, "invoice"
    if cfg.get("record_request_detection") and detect_record_request_page(full_text):
        return CODE_RECORD_REQUEST, "record_request"
    if cfg.get("instructions_detection") and detect_instructions_page(full_text):
        return CODE_INSTRUCTIONS, "instructions"
    if cfg.get("others_detection") and detect_others_page(full_text):
        return CODE_OTHERS, others_reason(full_text)
    return CODE_MAIN, ""


def fingerprint(text: str) -> str | None:
    """SHA-256 of whitespace-stripped lowercase OCR; None if too short.

    Kept for callers that still want an exact-content key. Live duplicate
    detection uses :func:`text_similarity` instead.
    """
    norm = normalize_duplicate_text(text)
    if len(norm) < _MIN_FINGERPRINT_CHARS:
        return None
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def normalize_duplicate_text(text: str) -> str:
    """Whitespace-stripped lowercase form used for duplicate comparisons."""
    return re.sub(r"\s+", "", text or "").lower()


def duplicate_char_count(text: str) -> int:
    """Comparable character count (same normalization as similarity)."""
    return len(normalize_duplicate_text(text))


def text_is_comparable(text: str) -> bool:
    """True when text is long enough to enter duplicate comparison."""
    return duplicate_char_count(text) >= DUPLICATE_MIN_CHARS


def text_is_contained(a: str, b: str) -> bool:
    """True when the shorter normalized text appears whole inside the longer."""
    na = normalize_duplicate_text(a)
    nb = normalize_duplicate_text(b)
    if not na or not nb:
        return False
    short, long = (na, nb) if len(na) <= len(nb) else (nb, na)
    return short in long


def _words(text: str) -> list[str]:
    return re.findall(r"\w+", (text or "").lower())


def sequence_similarity(a: str, b: str) -> float:
    """SequenceMatcher ratio over the two pages' words in order, in [0, 1].
    Order-sensitive: the same words read in another order score lower.

    Over words, not characters: a page's ~400 words match in about a millisecond
    where its ~3,000 characters took 30-60 ms, which made a chart of repeated forms
    take minutes. ``autojunk`` off: with it on, SequenceMatcher drops every item
    making up more than 1% of a long sequence, and a page 98% alike scored about 0.2
    (fixed 2026-10-22)."""
    from difflib import SequenceMatcher

    wa, wb = _words(a), _words(b)
    if not wa or not wb:
        return 0.0
    if wa == wb:
        return 1.0
    return SequenceMatcher(None, wa, wb, autojunk=False).ratio()


@lru_cache(maxsize=4096)
def _word_counts(text: str) -> "Counter[str]":
    """A page's word counts; each page meets up to 2 x ±window neighbors. Not mutated."""
    return Counter(_words(text))


def word_similarity(a: str, b: str) -> float:
    """Dice overlap of the two pages' word counts, in [0, 1]. Order-blind: OCR
    reading the same page's columns or boxes in another order still scores 1."""
    wa = _word_counts(a or "")
    wb = _word_counts(b or "")
    total = sum(wa.values()) + sum(wb.values())
    if not total or not wa or not wb:
        return 0.0
    return 2 * sum((wa & wb).values()) / total


def differing_words(a: str, b: str) -> int:
    """How many words differ between the two pages: the larger of the words only the
    first has and the words only the second has, so a replaced word counts once and an
    added word once. The absolute size of the difference, which a percentage hides on a
    long page."""
    wa, wb = _word_counts(a or ""), _word_counts(b or "")
    return max(sum((wa - wb).values()), sum((wb - wa).values()))


# "Page 15", "Page 1 of 7", "page 3/9".
_PAGE_MARKER = re.compile(r"\bpage\s*(\d{1,4})(?:\s*(?:of|/)\s*(\d{1,4}))?\b", re.IGNORECASE)


@lru_cache(maxsize=4096)
def page_markers(text: str) -> "Counter[str]":
    """The printed page numbers on a page ("15", "1/7"), counted. Two pages of a record
    that embed the same document share its "Page 1 of 7" but not the chart's own
    "Page 15" / "Page 27"; the same page sent twice carries the same numbers."""
    return Counter(
        f"{number}/{total}" if total else number
        for number, total in _PAGE_MARKER.findall(text or "")
    )


def similarity_parts(
    a: str, b: str, *, at_least: float = 0.0
) -> tuple[Optional[float], float, Optional[float], float]:
    """``(sequence, word, average, best possible average)`` for two pages' OCR text.

    The word-order match can never exceed the word-count match (its matched words
    are a share of the words the pages have in common), so the average is at most
    the word match. A pair whose word match is below ``at_least`` cannot reach it:
    it comes back with ``sequence`` and ``average`` None and the word match as the
    bound, without running SequenceMatcher. Otherwise the last value is the average.
    """
    na = normalize_duplicate_text(a)
    nb = normalize_duplicate_text(b)
    if na and na == nb:
        return 1.0, 1.0, 1.0, 1.0
    word = word_similarity(a, b)
    if word < at_least:
        return None, word, None, word
    sequence = sequence_similarity(a, b)
    average = (sequence + word) / 2
    return sequence, word, average, average


def text_similarity(a: str, b: str) -> float:
    """How alike two pages' OCR text is, in [0, 1]: the average of the
    order-sensitive word-sequence match and the order-blind word-count match, so
    neither a reordered read nor a run of shared boilerplate decides alone."""
    return similarity_parts(a, b)[2]


def classification_confidence(code: int, *, blank_via_image: bool = False) -> float | None:
    if code <= 0:
        return None
    if code == CODE_BLANK:
        return 0.95 if blank_via_image else 0.9
    if code == CODE_DUPLICATE:
        return 0.99
    return 0.85
