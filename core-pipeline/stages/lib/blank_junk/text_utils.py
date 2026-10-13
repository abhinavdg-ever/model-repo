"""Word-count and shared text helpers for junk classification."""

from __future__ import annotations

import re

from kw import rules

_WORD_RE = re.compile(r"\b\w+\b", re.UNICODE)
_ALPHA_WORD_RE = re.compile(r"[A-Za-z]{2,}")
_VOWEL_RE = re.compile(r"[aeiouAEIOU]")


def word_count(text: str) -> int:
    return len(_WORD_RE.findall(text or ""))


def _heading_pattern(heading: str) -> re.Pattern[str]:
    """The heading at the start of a line (after any markdown / numbering), then the
    end of the line or a colon / dash: "HPI", "## Assessment", "Chief Complaint: cough"."""
    words = r"\s+".join(re.escape(word) for word in heading.split())
    return re.compile(
        rf"^[\s#*>\-\d.)]*{words}\s*(?:$|[:\-\u2013])",
        re.IGNORECASE | re.MULTILINE,
    )


def clinical_headings(text: str) -> list[str]:
    """The distinct ``clinical_headings`` (junk_keywords_canon.json) that head a line."""
    return [
        heading
        for heading in rules().raw.get("clinical_headings") or []
        if _heading_pattern(heading).search(text or "")
    ]


def clinical_min_headings() -> int:
    return int(rules().raw.get("clinical_min_headings", 2))


def is_clinical_note(text: str) -> bool:
    """``clinical_min_headings`` (2) or more distinct clinical section headings."""
    return len(clinical_headings(text)) >= clinical_min_headings()


# A clinical note in the regex fallback: more than this many words, and headings.
CLINICAL_CONTENT_MIN_WORDS = 25


def looks_like_clinical_content(text: str) -> bool:
    """A real clinical note, so the junk rules should not fire: more than
    CLINICAL_CONTENT_MIN_WORDS words and ``clinical_min_headings`` (2) or more
    distinct clinical section headings. Clinical words inside sentences do not count."""
    return word_count(text) > CLINICAL_CONTENT_MIN_WORDS and is_clinical_note(text)


def is_signature_page(text: str) -> bool:
    return bool(rules().signature.search(text or ""))


def is_gibberish_ocr(
    text: str,
    *,
    min_alpha_words: int = 8,
    no_vowel_ratio: float = 0.45,
    weird_char_ratio: float = 0.22,
) -> bool:
    """True when Tesseract-like OCR looks mostly nonsensical.

    Heuristics (any one is enough once there is enough text):
      - Many letter-tokens with no vowels (classic Tess garbage)
      - High share of odd symbols vs letters/spaces
      - Very few vowel-bearing words among many tokens
    """
    body = text or ""
    if not body.strip():
        return False

    alpha_words = _ALPHA_WORD_RE.findall(body)
    if len(alpha_words) < min_alpha_words:
        return False

    no_vowel = sum(
        1 for w in alpha_words if len(w) >= 3 and not _VOWEL_RE.search(w)
    )
    if no_vowel / len(alpha_words) >= no_vowel_ratio:
        return True

    letters_spaces = sum(1 for c in body if c.isalpha() or c.isspace())
    weird = sum(
        1
        for c in body
        if not (c.isalnum() or c.isspace() or c in ".,;:!?-/()'\"%$#@&+")
    )
    if len(body) >= 40 and weird / len(body) >= weird_char_ratio:
        return True

    with_vowel = sum(1 for w in alpha_words if _VOWEL_RE.search(w))
    if with_vowel / len(alpha_words) <= 0.35 and len(alpha_words) >= 12:
        return True

    # Avoid false positive on dense clinical pages: need low letter density too
    if letters_spaces / max(len(body), 1) < 0.45 and len(alpha_words) >= 15:
        return True

    return False
