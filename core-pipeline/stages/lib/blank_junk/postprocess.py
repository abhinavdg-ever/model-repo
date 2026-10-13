"""Rules applied after the blank/junk model, in one place.

``apply`` takes the model's verdict for a page and returns the final one, in
this order:

  1. Clinical note -> Main: the model said Blank or Junk, but the page carries
     ``clinical_min_headings`` (2) or more distinct clinical section headings
     (``clinical_headings``: Chief Complaint, Assessment, Plan, …). A heading
     starts its own line, alone or as a label ("Chief Complaint: cough"); the
     same words inside a sentence do not count.
  2. Blank with text -> Junk: the model said Blank, the page has text, and the
     text does not say "blank" ("This page intentionally left blank"). Plain
     Junk, no sub-type.
  3. Short page (pass 2 only) -> Junk / Others: Main with fewer than
     SHORT_PAGE_WORDS words, not a signature page and not a clinical note.
  4. Signature page -> Main: a page under SHORT_PAGE_WORDS words that says
     "signature" (or that the key/value extraction found signed) is never
     Blank or Junk. A longer page keeps the model's verdict.
  5. Repeated junk keywords -> Junk (forced, last): a junk keyword category
     found ``forced_junk_min_hits`` (3) times or more makes the page Junk of
     that sub-type, whatever came before, clinical headings and a signature
     included. Categories: Letter/Fax (the word "fax" plus the ``letter_fax``
     patterns), Invoice, Record Request, Instructions, Others (table of
     contents). The cover-page words are left out: "summary" and "discharge
     summary" sit on real clinical pages.

The headings and the thresholds of rules 1 and 5 live in
``junk_keywords_canon.json``.
"""
from __future__ import annotations

import re
from typing import Optional

from classify import (
    CLASSIFICATION_LABELS,
    CODE_BLANK,
    CODE_INSTRUCTIONS,
    CODE_INVOICE,
    CODE_JUNK,
    CODE_LETTER_FAX,
    CODE_MAIN,
    CODE_OTHERS,
    CODE_RECORD_REQUEST,
)
from kw import rules
from text_utils import clinical_headings, clinical_min_headings, is_clinical_note, word_count

# --- 1. clinical note -------------------------------------------------------------
# clinical_headings / is_clinical_note live in text_utils: the regex fallback uses them too.


_FAX_WORD = re.compile(r"\bfax", re.IGNORECASE)


# --- 2. blank with text -----------------------------------------------------------

_BLANK_WORD = re.compile(r"\bblank\b", re.IGNORECASE)


def blank_with_text_is_junk(text: str) -> bool:
    return bool((text or "").strip()) and not _BLANK_WORD.search(text or "")


# --- 3. short page ----------------------------------------------------------------

SHORT_PAGE_WORDS = 20
_SIGNATURE_WORD = re.compile(r"\bsignature", re.IGNORECASE)


def short_page_is_junk(text: str, signed: bool) -> bool:
    """Too little text to be a page of a document, not a signature page and not a
    clinical note."""
    return (
        word_count(text) < SHORT_PAGE_WORDS
        and not is_signed(text, signed)
        and not is_clinical_note(text)
    )


# --- 4. signature page ------------------------------------------------------------

# The extraction skips blank/junk pages, so the word is what reaches them.
SIGNED_KEEP_REASON = (
    f"signature page (signed, < {SHORT_PAGE_WORDS} words) -> kept (overrules blank/junk)"
)


def is_signed(text: str, signed: bool) -> bool:
    return signed or bool(_SIGNATURE_WORD.search(text or ""))


def is_signature_page(text: str, signed: bool) -> bool:
    """A short page that is the signature: under SHORT_PAGE_WORDS words, signed."""
    return word_count(text) < SHORT_PAGE_WORDS and is_signed(text, signed)


# --- 5. repeated junk keywords ----------------------------------------------------

# Category in junk_keywords_canon.json -> (junk code, plain phrases?).
_FORCED_CATEGORIES = (
    ("letter_fax", CODE_LETTER_FAX, False),
    ("invoice", CODE_INVOICE, True),
    ("record_request", CODE_RECORD_REQUEST, True),
    ("instructions", CODE_INSTRUCTIONS, False),
    ("others_toc", CODE_OTHERS, False),
)


def _category_pattern(name: str, phrases: bool) -> re.Pattern[str]:
    items = rules().raw.get(name) or []
    parts = [rf"\b{re.escape(p)}\b" for p in items] if phrases else list(items)
    if name == "letter_fax":
        parts.append(_FAX_WORD.pattern)
    # Longest first, so "fax transmission" counts once, not also as "fax".
    parts.sort(key=len, reverse=True)
    return re.compile("|".join(f"(?:{p})" for p in parts), re.IGNORECASE)


def keyword_hits(text: str) -> list[tuple[int, str, int]]:
    """(code, category, hits) for every forced category found on the page."""
    found = []
    for name, code, phrases in _FORCED_CATEGORIES:
        hits = len(_category_pattern(name, phrases).findall(text or ""))
        if hits:
            found.append((code, name, hits))
    return found


def forced_junk(text: str) -> Optional[tuple[int, str]]:
    """(junk code, reason) when a category repeats ``forced_junk_min_hits``
    times or more; the category with the most hits wins."""
    limit = int(rules().raw.get("forced_junk_min_hits", 3))
    repeated = [hit for hit in keyword_hits(text) if hit[2] >= limit]
    if not repeated:
        return None
    code, name, hits = max(repeated, key=lambda hit: hit[2])
    return code, (
        f"forced junk: {name} keywords {hits} times (>= {limit}) "
        f"-> {CLASSIFICATION_LABELS[code]}"
    )


# --- all of them ------------------------------------------------------------------


def apply(
    code: int,
    reason: str,
    confidence: float,
    text: str,
    *,
    signed: bool = False,
    short_pages: bool = False,
) -> tuple[int, str, float]:
    """The model's (code, reason, confidence) -> the page's final verdict."""
    if code != CODE_MAIN:
        headings = clinical_headings(text)
        minimum = clinical_min_headings()
        if len(headings) >= minimum:
            code, confidence = CODE_MAIN, 0.9
            reason = (
                f"{reason}; clinical note: {len(headings)} section headings "
                f"(>= {minimum}: {', '.join(headings)}) -> kept"
            )
    if code == CODE_BLANK and blank_with_text_is_junk(text):
        code = CODE_JUNK
        reason = f"{reason}; blank page with text and no word 'blank' -> Junk"
    if short_pages and code == CODE_MAIN and short_page_is_junk(text, signed):
        code, confidence = CODE_OTHERS, 0.9
        reason = f"short page: {word_count(text)} words (< {SHORT_PAGE_WORDS}), not a signature page"
    if code != CODE_MAIN and is_signature_page(text, signed):
        code, confidence = CODE_MAIN, 0.9
        reason = f"{reason}; {SIGNED_KEEP_REASON}"
    forced = forced_junk(text)
    if forced is not None:
        return forced[0], f"{reason}; {forced[1]}", 0.95
    return code, reason, confidence
