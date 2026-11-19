"""Progress Note section headers (2026-11-19, user's list), matched loosely.

The list is the canon ``resources/canon/progress_note_headers.json`` (editable, reloads on
change). A line (or a stage-6 section header) is a Progress Note header when it is short (at most
max_words words, canon) and holds one of the sections' words, singular or plural, whatever else is on
it: "Orders:", "Orders Placed", "Plan of Care", "Treatment Plan", "Visit Diagnoses",
"Diagnosis", "Assessment & Plan". A sentence that only mentions a word ("we will plan ...")
is longer than a header and never counts. Used by blank / junk: the short page (a page that
continues the previous page's section, or carries a header and its words) and the clinical
content keep (2+ headers on a page).
"""
from __future__ import annotations

import re
from typing import Iterable, Optional

from modules.canon_store import CANON_DIR, CanonFile

_CANON: CanonFile[dict] = CanonFile(CANON_DIR / "progress_note_headers.json")
_CACHE: dict[str, object] = {}


def _settings() -> tuple[int, frozenset[str], list[tuple[str, re.Pattern[str]]]]:
    """(max words, weak sections, [(section, pattern)] longest name first) from the canon."""
    data = _CANON.get() or {}
    key = str(id(data))
    if _CACHE.get("key") != key:
        patterns = [
            (name, re.compile(r"(?<![A-Za-z])(?:" + "|".join(alts) + r")(?![A-Za-z])", re.IGNORECASE))
            for name, alts in (data.get("sections") or {}).items() if alts
        ]
        _CACHE.update(key=key, value=(int(data.get("max_words", 6)), frozenset(data.get("weak_sections") or ()),
                                      sorted(patterns, key=lambda item: -len(item[0]))))
    return _CACHE["value"]  # type: ignore[return-value]


_WORD = re.compile(r"[A-Za-z0-9]+")


def section_of(line: str) -> Optional[str]:
    """The Progress Note section a header line names, or None (a sentence, or no section word)."""
    max_words, _weak, patterns = _settings()
    text = " ".join(str(line or "").replace("#", " ").split()).strip(" :-*")
    words = _WORD.findall(text)
    if not words or len(words) > max_words:
        return None
    # Longer section names first, so "Assessment & Plan" is the combined section, not "Plan".
    for name, pattern in patterns:
        if pattern.search(text):
            return name
    return None


def header_lines(text: str, headings: Iterable[str] = ()) -> list[tuple[int, str, str]]:
    """(line index, line, section) for every Progress Note header: stage 6's section headers,
    and the page's own short lines (a header on a line of its own, or a "Label:" opening a line)."""
    out: list[tuple[int, str, str]] = []
    lines = (text or "").splitlines()
    for i, line in enumerate(lines):
        candidate = line.split(":", 1)[0] if ":" in line else line
        section = section_of(candidate)
        if section:
            out.append((i, line.strip(), section))
    seen = {section for _i, _l, section in out}
    for heading in headings or ():
        section = section_of(str(heading))
        if section and section not in seen:
            out.append((-1, str(heading), section))
            seen.add(section)
    return out


def sections_on(text: str, headings: Iterable[str] = ()) -> list[str]:
    """The distinct Progress Note sections the page has headers for, in page order."""
    return list(dict.fromkeys(section for _i, _l, section in header_lines(text, headings)))


def header_words(text: str, headings: Iterable[str] = ()) -> set[str]:
    """Every word of the page's Progress Note header lines (lower case)."""
    out: set[str] = set()
    for _i, line, _section in header_lines(text, headings):
        out.update(w.lower() for w in _WORD.findall(line.split(":", 1)[0] if ":" in line else line))
    return out


def clinical_sections_on(text: str, headings: Iterable[str] = ()) -> list[str]:
    """The page's Progress Note sections that only a note prints (the canon's weak_sections left out)."""
    weak = _settings()[1]
    return [s for s in sections_on(text, headings) if s not in weak]
