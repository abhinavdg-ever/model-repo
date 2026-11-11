"""Duplicate pages, one chart at a time (2026-11-11, user: Duplicate_Logic.md).

1. **Prepare.** Only Main pages are compared (never blank or junk). Each page's header and
   footer lines are left out: print stamps, "Page x of y", fax lines, and the header / footer
   lines repeated on ``repeated_line_pages`` (3) or more pages of the chart. The body is
   what is compared.
2. **Matches.** Each page is compared with its ``matches_per_page`` (3) most similar earlier
   pages (and the earliest page whose text is exactly its own). The earlier page is the
   original, the later page the possible duplicate.
3. **Hard blocks** (nothing overrides one): length (word counts more than 10% or more than 20
   words apart); dates and numbers (any differing word is a date, time or number); signature
   (the signature blocks differ); short signed page (either page signed with under 25 words);
   image (either page under 25 words, a form or handwritten, and the page images do not
   match). The date-of-service block needs the documents, so continuity applies it
   (``modules/continuity/stage.py``, ``dos_block``).
4. **Exact.** The same text (single stray letters and punctuation aside) at any distance.
5. **Within ``near_pages`` (3)** only an exact match counts.
6. **Runs, more than 3 pages apart, in the same order** (page k copies k - d, k + 1 copies
   k + 1 - d): Rule A -- a page next to a duplicate of the run that matches at least 98% is a
   duplicate too, until the run stops growing; Rule B -- three pages in a row averaging at
   least 95%, none below 90%, are all duplicates.
7. **Conflicts.** Two pages that are partial (not exact) matches to the same original: neither
   is a duplicate, both are flagged "Conflict". Several exact copies are all duplicates.
8. **Labels.** "Duplicate of N" for a duplicate; "Copy of N" for a 100% text match a hard
   block kept from being a duplicate (it stays a normal page). N is always the earliest page.

The score of a pair is the overlap of the two bodies' distinct words.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

# Settings (canon ``duplicate_logic`` overrides any of them).
DEFAULTS: dict[str, float] = {
    "near_pages": 3,
    "matches_per_page": 3,
    "short_words": 25,
    "length_share": 0.10,
    "length_words": 20,
    "rule_a_min": 0.98,
    "rule_b_average": 0.95,
    "rule_b_min": 0.90,
    "repeated_line_pages": 3,
    "head_lines": 4,
    "foot_lines": 3,
    # Two page images match when this share of their ink lies on the other's (a pixel of give).
    "image_min_match": 0.90,
}

EXACT, RULE_A, RULE_B = "Exact", "Rule A", "Rule B"
CONFLICT = "Conflict"

BLOCK_LENGTH = "length"
BLOCK_NUMBERS = "dates and numbers"
BLOCK_SIGNATURE = "signature"
BLOCK_SHORT_SIGNED = "short signed page"
BLOCK_IMAGE = "image"
BLOCK_DOS = "date of service"

_PAGINATION = re.compile(r"\b(?:page|pg)\.?\s*[:#]?\s*\d{1,4}\s*(?:of|/)\s*\d{1,4}\b|^\s*\d{1,4}\s+of\s+\d{1,4}\s*$", re.I)
_STAMP = re.compile(
    r"\b(?:printed|print\s+date|printed\s+(?:on|by)|generated(?:\s+on)?|run\s+date|report\s+date|"
    r"date\s+printed|date/time\s+printed)\b.*\d", re.I)
_FAX = re.compile(r"\bfax\b.*\d|\d.*\bfax\b|\bp\.\s*\d{1,4}\s*/\s*\d{1,4}\b|\bpage\s+\d{3}\b", re.I)
_TOKEN = re.compile(r"[a-z0-9]+")
_MONTHS = frozenset({
    "jan", "january", "feb", "february", "mar", "march", "apr", "april", "may", "jun", "june",
    "jul", "july", "aug", "august", "sep", "sept", "september", "oct", "october", "nov",
    "november", "dec", "december", "am", "pm",
})


def settings(overrides: Optional[dict[str, Any]] = None) -> dict[str, float]:
    out = dict(DEFAULTS)
    for key, value in (overrides or {}).items():
        if key in out and isinstance(value, (int, float)):
            out[key] = value
    return out


def _line_key(line: str) -> str:
    return re.sub(r"\s+", " ", line.strip().lower())


def _lines(text: str) -> list[str]:
    return [line for line in (text or "").splitlines() if line.strip()]


# A header / footer line is short; a page no longer than its bands has no header or footer.
BAND_LINE_MAX_CHARS = 150


def _band(lines: list[str], head: int, foot: int) -> set[int]:
    if len(lines) <= head + foot:
        return set()
    return {i for i in (*range(head), *range(len(lines) - foot, len(lines)))
            if len(lines[i].strip()) <= BAND_LINE_MAX_CHARS}


def repeated_band_lines(texts: Sequence[str], cfg: dict[str, float]) -> frozenset[str]:
    """Header / footer lines (normalised) found on ``repeated_line_pages`` or more pages."""
    pages_with: dict[str, int] = {}
    for text in texts:
        lines = _lines(text)
        keys = {_line_key(lines[i]) for i in _band(lines, int(cfg["head_lines"]), int(cfg["foot_lines"]))}
        for key in keys:
            if key:
                pages_with[key] = pages_with.get(key, 0) + 1
    return frozenset(key for key, n in pages_with.items() if n >= int(cfg["repeated_line_pages"]))


def is_boilerplate(line: str) -> bool:
    """A print stamp, "Page x of y" or a fax line."""
    return bool(_PAGINATION.search(line) or _STAMP.search(line) or _FAX.search(line))


def body_text(text: str, repeated: frozenset[str], cfg: dict[str, float]) -> str:
    """The page without its header and footer lines."""
    lines = _lines(text)
    band = _band(lines, int(cfg["head_lines"]), int(cfg["foot_lines"]))
    keep = [line for i, line in enumerate(lines)
            if not is_boilerplate(line) and not (i in band and _line_key(line) in repeated)]
    return "\n".join(keep)


@dataclass
class Body:
    tokens: tuple[str, ...]
    words: frozenset[str]
    exact_key: tuple[str, ...]

    @property
    def count(self) -> int:
        return len(self.tokens)


def body(text: str) -> Body:
    tokens = tuple(_TOKEN.findall((text or "").lower()))
    # Single stray letters (OCR specks, "|" read as "l") do not make two texts different.
    return Body(tokens, frozenset(tokens), tuple(t for t in tokens if not (len(t) == 1 and t.isalpha())))


def score(a: Body, b: Body) -> float:
    """Shared distinct words / all distinct words of the two bodies."""
    union = len(a.words | b.words)
    return len(a.words & b.words) / union if union else 0.0


def is_date_or_number(word: str) -> bool:
    return any(ch.isdigit() for ch in word) or word in _MONTHS


@dataclass
class Page:
    """One comparable (Main) page. ``position`` is its index in the chart's page order."""

    page_id: int
    position: int
    text: str
    signed: bool = False
    # kv_extract's signature: (provider name, signature date), normalised; ("", "") unknown.
    signature: tuple[str, str] = ("", "")
    needs_image: bool = False  # a form or a handwritten page
    image: Optional[Path] = None


@dataclass
class Outcome:
    """What one later page is. ``original`` is the earliest matching page."""

    page_id: int
    label: str = ""  # "duplicate", "copy" or ""
    original: Optional[int] = None
    rule: str = ""
    score: float = 0.0
    blocked_by: str = ""
    review: str = ""
    conflict_with: Optional[int] = None
    distance: int = 0
    # The closest page looked at (any score), for blank_junk.json.
    closest: Optional[dict[str, Any]] = None

    @property
    def is_duplicate(self) -> bool:
        return self.label == "duplicate"


@dataclass
class _Pair:
    original: int
    score: float
    exact: bool
    block: str


ImageMatch = Callable[[Optional[Path], Optional[Path]], bool]


def find(pages: Sequence[Page], *, all_texts: Optional[Sequence[str]] = None,
         cfg: Optional[dict[str, float]] = None, near_only: bool = False,
         image_match: Optional[ImageMatch] = None) -> dict[int, Outcome]:
    """Every comparable page's outcome (page id -> Outcome), pages in chart order.

    ``all_texts``: every page's text in the chart (for the header / footer lines repeated on 3
    or more pages); the comparable pages' own when not given. ``near_only`` (blank / junk pass
    1): exact matches within ``near_pages`` only, pages under ``short_words`` left for pass 2.
    """
    cfg = cfg or settings()
    match_images = image_match or (lambda a, b: images_match(a, b, float(cfg["image_min_match"])))
    repeated = repeated_band_lines(all_texts if all_texts is not None else [p.text for p in pages], cfg)
    bodies = {p.page_id: body(body_text(p.text, repeated, cfg)) for p in pages}
    by_id = {p.page_id: p for p in pages}
    at = {p.position: p.page_id for p in pages}
    short = int(cfg["short_words"])
    near = int(cfg["near_pages"])

    def block(orig: int, copy: int) -> str:
        a, b = bodies[orig], bodies[copy]
        pa, pb = by_id[orig], by_id[copy]
        big, small = max(a.count, b.count), min(a.count, b.count)
        if big - small > cfg["length_words"] or big - small > cfg["length_share"] * big:
            return BLOCK_LENGTH
        if any(is_date_or_number(w) for w in a.words ^ b.words):
            return BLOCK_NUMBERS
        if pa.signed != pb.signed or (pa.signed and pa.signature != pb.signature):
            return BLOCK_SIGNATURE
        if (pa.signed and a.count < short) or (pb.signed and b.count < short):
            return BLOCK_SHORT_SIGNED
        if (small < short or pa.needs_image or pb.needs_image) and not match_images(pa.image, pb.image):
            return BLOCK_IMAGE
        return ""

    # Step 2: each page's three most similar earlier pages, plus its earliest exact match.
    pairs: dict[int, dict[int, _Pair]] = {}
    first_exact: dict[tuple[str, ...], int] = {}
    seen: list[Page] = []
    outcomes: dict[int, Outcome] = {}
    for page in pages:
        mine = bodies[page.page_id]
        out = outcomes[page.page_id] = Outcome(page.page_id)
        if not mine.tokens or (near_only and mine.count < short):
            continue
        top: list[tuple[float, int, int]] = []  # (score, -position, page id)
        k = int(cfg["matches_per_page"])
        for other in seen:
            if near_only and page.position - other.position > near:
                continue
            theirs = bodies[other.page_id]
            lo, hi = sorted((len(mine.words), len(theirs.words)))
            if len(top) >= k and (lo / hi if hi else 0.0) < top[-1][0]:
                continue  # cannot beat the third best
            s = score(mine, theirs)
            top.append((s, -other.position, other.page_id))
            top.sort(reverse=True)
            del top[k:]
        chosen = {pid for _, _, pid in top}
        exact_id = first_exact.get(mine.exact_key)
        if exact_id is not None and not (near_only and page.position - by_id[exact_id].position > near):
            chosen.add(exact_id)
        found: dict[int, _Pair] = {}
        for pid in chosen:
            exact = bodies[pid].exact_key == mine.exact_key
            found[pid] = _Pair(pid, 1.0 if exact else score(mine, bodies[pid]), exact, block(pid, page.page_id))
        pairs[page.page_id] = found
        if found:
            best = max(found.values(), key=lambda p: (p.score, -by_id[p.original].position))
            out.closest = {"page_id": best.original, "similarity": round(best.score, 4),
                           "exact": best.exact, "blocked_by": best.block}
        first_exact.setdefault(mine.exact_key, page.page_id)
        seen.append(page)

    dup: dict[int, tuple[int, str, float]] = {}  # page -> (original, rule, score)
    # Step 4 / 5: exact, at any distance; the earliest unblocked exact page.
    for page in pages:
        exact = sorted((p for p in pairs.get(page.page_id, {}).values() if p.exact),
                       key=lambda p: by_id[p.original].position)
        clear = [p for p in exact if not p.block]
        if clear:
            dup[page.page_id] = (clear[0].original, EXACT, 1.0)
        elif exact:
            out = outcomes[page.page_id]
            out.label, out.original, out.score, out.blocked_by = "copy", exact[0].original, 1.0, exact[0].block

    if not near_only:
        # Step 6: runs more than ``near`` pages apart, in the same order.
        def pair_at(position: int, distance: int) -> Optional[_Pair]:
            pid, orig = at.get(position), at.get(position - distance)
            if pid is None or orig is None:
                return None
            found = pairs.get(pid, {}).get(orig)
            return found if found is not None and not found.block else None

        offset: dict[int, int] = {}
        for pid, (orig, _, _) in dup.items():
            offset[pid] = by_id[pid].position - by_id[orig].position
        grow: list[tuple[int, int]] = [(by_id[pid].position, d) for pid, d in offset.items() if d > near]
        # Rule B: three in a row, average >= 95%, none below 90%.
        for page in pages:
            for p in pairs.get(page.page_id, {}).values():
                d = page.position - by_id[p.original].position
                if d <= near:
                    continue
                trio = [pair_at(page.position + t, d) for t in range(3)]
                if any(t is None for t in trio):
                    continue
                scores = [t.score for t in trio]  # type: ignore[union-attr]
                if min(scores) < cfg["rule_b_min"] or sum(scores) / 3 < cfg["rule_b_average"]:
                    continue
                for t, item in enumerate(trio):
                    pid = at[page.position + t]
                    if pid not in dup:
                        dup[pid] = (item.original, RULE_B, item.score)  # type: ignore[union-attr]
                        offset[pid] = d
                        grow.append((page.position + t, d))
        # Rule A: next to a duplicate of the run, at least 98%; until the run stops growing.
        while grow:
            position, d = grow.pop()
            for step in (-1, 1):
                n = position + step
                pid = at.get(n)
                if pid is None or pid in dup:
                    continue
                item = pair_at(n, d)
                if item is not None and item.score >= cfg["rule_a_min"]:
                    dup[pid] = (item.original, RULE_A, item.score)
                    offset[pid] = d
                    grow.append((n, d))

    def earliest(pid: int) -> int:
        seen_ids: set[int] = set()
        while pid in dup and pid not in seen_ids:
            seen_ids.add(pid)
            pid = dup[pid][0]
        return pid

    # Step 7: two partial matches to one original -- neither is a duplicate.
    partial_by_original: dict[int, list[int]] = {}
    for pid, (orig, rule, _) in dup.items():
        if rule != EXACT:
            partial_by_original.setdefault(earliest(orig), []).append(pid)
    conflicted: dict[int, int] = {}
    for orig, group in partial_by_original.items():
        if len(group) > 1:
            group.sort(key=lambda pid: by_id[pid].position)
            for pid in group:
                conflicted[pid] = next(other for other in group if other != pid)

    for pid, (orig, rule, s) in dup.items():
        out = outcomes[pid]
        if pid in conflicted:
            out.label, out.review, out.conflict_with = "", CONFLICT, conflicted[pid]
            out.original, out.rule, out.score = earliest(orig), rule, s
            continue
        out.label, out.rule, out.score, out.blocked_by = "duplicate", rule, s, ""
        out.original = earliest(orig)
        out.distance = by_id[pid].position - by_id[orig].position
    for out in outcomes.values():
        if out.label == "copy" and out.original is not None:
            out.original = earliest(out.original)
    return outcomes


# ---- page images (block 6) -------------------------------------------------------------

IMAGE_SIDE = 96


@lru_cache(maxsize=512)
def _ink(path: str) -> Optional[Any]:
    """The page's ink, cropped to where it is and scaled to IMAGE_SIDE square (numpy bool)."""
    try:
        import numpy as np
        from PIL import Image, ImageOps

        with Image.open(path) as image:
            gray = ImageOps.autocontrast(image.convert("L"))
        gray.thumbnail((800, 800))
        mask = np.asarray(gray) < 128
        rows, cols = np.where(mask)
        if not len(rows):
            return np.zeros((IMAGE_SIDE, IMAGE_SIDE), dtype=bool)
        crop = gray.crop((int(cols.min()), int(rows.min()), int(cols.max()) + 1, int(rows.max()) + 1))
        small = crop.resize((IMAGE_SIDE, IMAGE_SIDE), Image.BILINEAR)
        return np.asarray(small) < 160
    except Exception:  # noqa: BLE001 -- an unreadable image never matches
        return None


def _grow(mask: Any) -> Any:
    import numpy as np

    out = mask.copy()
    out[1:, :] |= mask[:-1, :]
    out[:-1, :] |= mask[1:, :]
    out[:, 1:] |= mask[:, :-1]
    out[:, :-1] |= mask[:, 1:]
    return np.asarray(out)


def image_similarity(a: Optional[Path], b: Optional[Path]) -> Optional[float]:
    """The share of the two pages' ink that lies on the other's (one pixel of give), or None."""
    if a is None or b is None:
        return None
    ink_a, ink_b = _ink(str(a)), _ink(str(b))
    if ink_a is None or ink_b is None:
        return None
    total = int(ink_a.sum()) + int(ink_b.sum())
    if not total:
        return 1.0
    hit = int((ink_a & _grow(ink_b)).sum()) + int((ink_b & _grow(ink_a)).sum())
    return hit / total


def images_match(a: Optional[Path], b: Optional[Path], minimum: float = DEFAULTS["image_min_match"]) -> bool:
    found = image_similarity(a, b)
    return found is not None and found >= minimum
