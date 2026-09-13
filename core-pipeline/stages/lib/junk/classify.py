"""Junk classification codes + text rules."""

from __future__ import annotations

import hashlib
import re

from blank import detect_blank_page, is_declared_blank_page
from cover import detect_cover_page
from instructions import detect_instructions_page
from invoice import detect_invoice_page
from letter_fax import detect_letter_fax_page
from others import detect_others_page, others_reason
from record_request import detect_record_request_page

CODE_MAIN = 0
CODE_BLANK = 1
CODE_INVOICE = 2
CODE_COVER_PAGE = 3
CODE_DUPLICATE = 4
CODE_RECORD_REQUEST = 5
CODE_INSTRUCTIONS = 6
CODE_OTHERS = 7
CODE_LETTER_FAX = 8

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
    }
)

_MIN_FINGERPRINT_CHARS = 50

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
    """
    cfg = {**DEFAULT_SETTINGS, **(settings or {})}

    if cfg.get("blank_detection") and detect_blank_page(full_text):
        return CODE_BLANK, "blank_ocr"
    if cfg.get("declared_blank_detection") and is_declared_blank_page(text=full_text):
        return CODE_BLANK, "declared_blank"
    if cfg.get("cover_page_detection") and detect_cover_page(full_text):
        return CODE_COVER_PAGE, "cover_page"
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
    """SHA-256 of whitespace-stripped lowercase OCR; None if too short."""
    norm = re.sub(r"\s+", "", text or "").lower()
    if len(norm) < _MIN_FINGERPRINT_CHARS:
        return None
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def classification_confidence(code: int, *, blank_via_image: bool = False) -> float | None:
    if code <= 0:
        return None
    if code == CODE_BLANK:
        return 0.95 if blank_via_image else 0.9
    if code == CODE_DUPLICATE:
        return 0.99
    return 0.85
