"""Shared page detectors (Brief 5 section 3, 2026-11-22): a page's note sections, its note start
pattern, its section ranks, its lab markers and whether it is a visual page. One module for the
classifier, the Final step, continuity and sequencing -- no copies.

* **Sections** use the one Progress Note header matcher (``blank_junk.pn_headers.section_of``,
  regexes in ``progress_note_headers.json``): a line whose part before ':' (or the whole line, at
  most ``max_words`` words) names a section, or an OCR-detected heading. Signature lines, list
  items, table cells and lines the caller marks as running header / footer never count.
* **Ranks** follow the usual note order in ``page_arbitration.json`` -> ``sections.rank``.
* **Lab markers** (``lab_markers``): stand-alone report markers (performing lab, CLIA, accession,
  a specimen block, "Final report") and embedded result headings.
* **Visual** (``visual_page``): picture area >= picture_share of the page, or fewer than
  max_words OCR words; never a blank / junk / duplicate page.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Iterable, Optional

from modules.canon_store import CANON_DIR, CanonFile

_ARBITRATION: CanonFile[dict[str, Any]] = CanonFile(CANON_DIR / "page_arbitration.json")
_LIST_ITEM = re.compile(r"^\s*(?:[•▪●◦‣⁃·*\-–o]\s+|\d+[.)]\s+)")
_WORD = re.compile(r"[A-Za-z0-9]+")
VISIT_CC, VISIT_SOAP, VISIT_HISTORY_FIRST = "visit_cc", "visit_soap", "visit_history_first"
_SOAP = re.compile(r"(?i)^\s*(?:subjective|s)\s*:")


def _settings(name: str) -> dict[str, Any]:
    return dict(_ARBITRATION.get().get(name) or {})


@dataclass(frozen=True)
class Section:
    name: str  # the generic section (progress_note_headers.json)
    line: int  # its line index on the page
    n_lines: int  # lines until the next section (or the page end)


def _lines(text: str) -> list[str]:
    return (text or "").splitlines()


def _is_signature(line: str) -> bool:
    from modules.continuity.signature import has_signature_section

    return has_signature_section(line)


def _heading_name(line: str) -> Optional[str]:
    """The section a line heads: its part before ':' (or the whole short line)."""
    from modules.blank_junk.pn_headers import section_of

    raw = line.strip()
    if not raw or "|" in raw or _LIST_ITEM.match(raw) or _is_signature(raw):
        return None
    head = raw.split(":", 1)[0] if ":" in raw else raw
    return section_of(head.lstrip("#").strip())


def note_sections(text: str, headings: Iterable[str] = (), exclude: Iterable[str] = ()) -> list[Section]:
    """The page's note sections in page order (each generic section once, at its first line).
    ``headings``: the OCR-detected headings; ``exclude``: lines that are the running header /
    footer (continuity's bands), which never count."""
    from modules.blank_junk.pn_headers import section_of

    lines = _lines(text)
    skip = {" ".join(x.split()).lower() for x in exclude if x}
    found: dict[str, int] = {}
    for i, line in enumerate(lines):
        if " ".join(line.split()).lower() in skip:
            continue
        name = _heading_name(line)
        if name and name not in found:
            found[name] = i
    for heading in headings or ():
        name = section_of(str(heading))
        if name and name not in found and " ".join(str(heading).split()).lower() not in skip:
            where = next((i for i, line in enumerate(lines) if str(heading).strip() and str(heading).strip() in line), len(lines))
            found[name] = where
    ordered = sorted(found.items(), key=lambda kv: kv[1])
    out = []
    for k, (name, at) in enumerate(ordered):
        end = ordered[k + 1][1] if k + 1 < len(ordered) else len(lines)
        out.append(Section(name, at, max(1, end - at)))
    return out


def rank_of(section: str) -> Optional[int]:
    return (_settings("sections").get("rank") or {}).get(section)


def first_section_rank(text: str, headings: Iterable[str] = ()) -> Optional[int]:
    ranks = [r for r in (rank_of(s.name) for s in note_sections(text, headings)) if r is not None]
    return ranks[0] if ranks else None


def last_section_rank(text: str, headings: Iterable[str] = ()) -> Optional[int]:
    ranks = [r for r in (rank_of(s.name) for s in note_sections(text, headings)) if r is not None]
    return ranks[-1] if ranks else None


@lru_cache(maxsize=64)
def _visit_header_patterns(key: str) -> tuple[re.Pattern[str], ...]:
    from modules.page_classify import leaf_canon

    rules = (leaf_canon.canon().priority or {}).get("rules") or ()
    rule = next((r for r in rules if r.get("name") == "progress_note_headers"), {})
    return tuple(re.compile(p, re.I) for p in rule.get("visit_header_patterns") or ())


def visit_header(text: str) -> bool:
    """A visit header in the top zone (the family priority rule's visit-header patterns)."""
    top = "\n".join(_lines(text)[: int(_settings("sections").get("top_zone_lines", 12))])
    return any(p.search(top) for p in _visit_header_patterns("v1"))


def start_pattern(text: str, headings: Iterable[str] = ()) -> Optional[str]:
    """How a visit note starts on this page: a visit header followed by CC / reason / HPI
    (visit_cc), by Subjective / "S:" (visit_soap), or by history lists before any CC
    (visit_history_first); None when the page does not open a note."""
    if not visit_header(text):
        return None
    if any(_SOAP.match(line) for line in _lines(text)[:20]):
        return VISIT_SOAP
    secs = note_sections(text, headings)
    if not secs:
        return None
    first = rank_of(secs[0].name)
    if secs[0].name in ("Chief complaint", "History of present illness"):
        return VISIT_CC
    if first == int(_settings("sections").get("history_rank", 4)):
        return VISIT_HISTORY_FIRST
    return None


@lru_cache(maxsize=8)
def _lab_patterns(key: str) -> tuple[tuple[str, re.Pattern[str]], ...]:
    std = (_settings("lab_markers").get("standalone") or {})
    return tuple((name, re.compile(rx, re.I | re.M)) for name, rx in std.items())


def standalone_lab_markers(text: str) -> list[str]:
    """The stand-alone lab report markers on the page (each named once)."""
    return [name for name, rx in _lab_patterns("v1") if rx.search(text or "")]


def embedded_result_markers(text: str, previous_note_text: Optional[str] = None) -> list[str]:
    """Embedded result markers: an embedded results heading, and the previous Progress Note
    page's running header (its first line) repeated at the top of this page."""
    from modules.page_classify.arbitration import heading_regex

    names = _settings("lab_markers").get("embedded_headings") or ()
    out = []
    if names and heading_regex(tuple(names)).search(text or ""):
        out.append("result_heading")
    if previous_note_text:
        prev = next((" ".join(l.split()).lower() for l in _lines(previous_note_text) if l.strip()), "")
        top = [" ".join(l.split()).lower() for l in _lines(text) if l.strip()][:3]
        if prev and prev in top:
            out.append("same_running_header")
    return out


def is_visual(text: str, picture_share: Optional[float] = None, kind: str = "main") -> bool:
    """A visual page: picture area >= picture_share of the page, or fewer than max_words words;
    never a blank / junk / duplicate page."""
    if kind != "main":
        return False
    cfg = _settings("visual_page")
    if picture_share is not None and picture_share >= float(cfg.get("picture_share", 0.6)):
        return True
    return len((text or "").split()) < int(cfg.get("max_words", 25))
