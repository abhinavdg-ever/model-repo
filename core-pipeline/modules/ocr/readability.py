"""Readability (2026-11-28, user; was Visibility): ``is_readable`` Yes / No per page.

No when the page-tag model calls the page not visible, or when all three hold:

* Page Quality is Low (the routing's Final Quality);
* the OCR match rate is under 50% (the three engines' when Doc Intel read the page, else
  Docling vs Tesseract);
* Tesseract's text is gibberish.

Gibberish (2026-11-28, user: both tests combined). Tesseract's tokens, numbers / dates /
punctuation left out, are scored twice:

* ``dictionary_share``: tokens in the English word list (pyspellchecker, offline) or in our
  own canon vocabulary (every word in ``resources/canon/*.json``: page keywords, section
  headers, credentials, lab and medication terms ...);
* ``shape_share``: tokens shaped like a word (letters only, a vowel, normal casing).

The page is gibberish when the average of the two is under GIBBERISH_BELOW (50%). Under
MIN_TOKENS tokens nothing is judged (None). Without pyspellchecker the canon words alone are
the dictionary.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from typing import Any, Optional

GIBBERISH_BELOW = 0.5
MATCH_BELOW = 0.5
MIN_TOKENS = 20
_TOKEN = re.compile(r"[A-Za-z][A-Za-z'\-]*")


def _tokens(text: str) -> list[str]:
    out = []
    for raw in (text or "").split():
        token = raw.strip(".,;:!?()[]{}'\"|_*#")
        if not token or re.fullmatch(r"[\d/\-.:#$%,+]+", token):
            continue
        out.append(token)
    return out


def _shaped(token: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z][a-z]+|[A-Z]{2,}", token) and re.search(r"[aeiouyAEIOUY]", token))


@lru_cache(maxsize=1)
def _canon_words() -> frozenset[str]:
    from modules.canon_store import CANON_DIR

    words: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, str):
            words.update(w.lower() for w in _TOKEN.findall(value) if len(w) > 1)
        elif isinstance(value, dict):
            for key, item in value.items():
                walk(key)
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    for path in sorted(CANON_DIR.glob("*.json")):
        try:
            walk(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return frozenset(words)


@lru_cache(maxsize=1)
def _english() -> Any:
    try:
        from spellchecker import SpellChecker

        return SpellChecker(language="en").word_frequency
    except Exception:  # noqa: BLE001 -- the canon words alone then
        return None


def _known(token: str) -> bool:
    word = token.lower().strip("'-")
    if not word:
        return False
    if word in _canon_words():
        return True
    english = _english()
    if english is not None and word in english:
        return True
    # Hyphenated words: every part known.
    parts = [p for p in word.split("-") if p]
    return len(parts) > 1 and all(_known(p) for p in parts)


def gibberish(text: Optional[str]) -> dict[str, Any]:
    """{"tokens", "dictionary_share", "shape_share", "score", "gibberish"}; shares and the verdict
    are None under MIN_TOKENS tokens."""
    tokens = _tokens(text or "")
    out: dict[str, Any] = {"tokens": len(tokens), "dictionary_share": None, "shape_share": None,
                           "score": None, "gibberish": None}
    if len(tokens) < MIN_TOKENS:
        return out
    dictionary = sum(1 for t in tokens if _known(t)) / len(tokens)
    shape = sum(1 for t in tokens if _shaped(t)) / len(tokens)
    score = (dictionary + shape) / 2
    out.update(dictionary_share=round(dictionary, 4), shape_share=round(shape, 4), score=round(score, 4),
               gibberish=score < GIBBERISH_BELOW)
    return out


def is_readable(*, is_visible: Optional[bool], final_quality: Optional[str], match_rate: Optional[float],
                tesseract_gibberish: Optional[bool]) -> tuple[bool, str]:
    """(is_readable, why not)."""
    if is_visible is False:
        return False, "not visible (page-tag model)"
    low = (final_quality or "").strip().lower() == "low"
    bad_match = match_rate is not None and float(match_rate) < MATCH_BELOW
    if low and bad_match and tesseract_gibberish:
        return False, f"Page Quality Low, OCR match {float(match_rate):.0%}, Tesseract gibberish"
    return True, ""
