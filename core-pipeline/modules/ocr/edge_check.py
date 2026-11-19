"""Docling's first and last row against Tesseract's (2026-11-19, user).

Docling sometimes drops a page's top (or bottom) row, or runs its words together
("Webb, PaulDate of Birth:"). Tesseract reads that row as one line. Per edge:

  match           the row is in Docling as Tesseract has it (or 80%+ of its words are there:
                  a letter read differently is not a missing row)
  spacing_fixed   the row is in Docling letter for letter but split or run together
                  differently: Docling's span is replaced by Tesseract's line
  missing         the row is not in Docling: the routing caps the match rate under 90%,
                  so the page is Medium and goes to Doc Intel (modules/ocr/routing.py)
  skipped         Tesseract's row has under 3 words: nothing to compare

Only on pages whose Tesseract text is trusted (printed, not low quality). No text is logged.
"""
from __future__ import annotations

import re
from typing import Optional

MIN_WORDS = 3
COVERAGE_MATCH = 0.80
# How far from Docling's start (top) or end (bottom) the row may sit, in letters and digits:
# at least this, or 3 x the row's own length.
EDGE_REACH = 200

_WORD = re.compile(r"[a-z0-9]+")
_ALNUM = re.compile(r"[A-Za-z0-9]")

STATUSES = ("match", "spacing_fixed", "missing", "skipped")


def _words(text: str) -> list[str]:
    return _WORD.findall((text or "").lower())


def _compact(text: str) -> tuple[str, list[int]]:
    """Lower-case letters and digits only, with each one's position in ``text``."""
    chars, where = [], []
    for i, ch in enumerate(text or ""):
        if _ALNUM.match(ch):
            chars.append(ch.lower())
            where.append(i)
    return "".join(chars), where


def edge_lines(tesseract: str) -> tuple[Optional[str], Optional[str]]:
    """Tesseract's first and last non-empty lines."""
    lines = [line.strip() for line in (tesseract or "").splitlines() if line.strip()]
    if not lines:
        return None, None
    return lines[0], lines[-1]


def _check_edge(docling: str, line: Optional[str], top: bool) -> tuple[str, str]:
    """(status, docling text, spacing fixed when the status says so) for one edge."""
    if not line or len(_words(line)) < MIN_WORDS:
        return "skipped", docling
    want, _ = _compact(line)
    have, where = _compact(docling)
    if not want or not have:
        return "missing", docling
    reach = max(EDGE_REACH, 3 * len(want))
    start = have.find(want) if top else have.rfind(want)
    near = start >= 0 and (start <= reach if top else len(have) - (start + len(want)) <= reach)
    if near:
        lo, hi = where[start], where[start + len(want) - 1] + 1
        # Widen to whole words: punctuation round the row stays as Docling wrote it.
        if _words(docling[lo:hi]) == _words(line):
            return "match", docling
        return "spacing_fixed", docling[:lo] + line + docling[hi:]
    words = _words(line)
    region = _words(docling)
    span = 3 * len(words) + 20
    region = region[:span] if top else region[-span:]
    have_words = set(region)
    if sum(1 for w in words if w in have_words) / len(words) >= COVERAGE_MATCH:
        return "match", docling
    return "missing", docling


def check(docling: str, tesseract: str) -> tuple[str, dict[str, str]]:
    """Docling's text with any spacing-only edge row replaced by Tesseract's, and the status
    of each edge: {"top": ..., "bottom": ...}."""
    first, last = edge_lines(tesseract)
    top, fixed = _check_edge(docling or "", first, top=True)
    bottom, fixed = _check_edge(fixed, last, top=False) if last != first else ("skipped", fixed)
    return fixed, {"top": top, "bottom": bottom}


def fix_text(text: str, tesseract: str) -> str:
    """The same spacing fixes on another rendering of the page (Docling's markdown)."""
    return check(text, tesseract)[0]


def edge_missing(status: Optional[dict[str, str]]) -> bool:
    return bool(status) and "missing" in (status.get("top"), status.get("bottom"))
