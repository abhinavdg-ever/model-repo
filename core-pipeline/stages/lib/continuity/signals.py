"""The measurements behind the gap signals (docs/APPROACH.md §4).

Pure functions on page lines and text. The engine turns each into points
(``continuity_canon.json`` ``points``). Text similarity is a small TF-IDF written here (no
new dependency): character 3–5 grams inside words and word 1–2 grams, ``min_df`` 2,
sublinear tf, fitted on every tail / head block of the chart (never per pair: idf needs
the corpus to discount the vocabulary every page shares).
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any, Optional, Sequence

# 'Visit #: 12345', 'Encounter No 9988-1', 'FIN 100234', 'CSN: 4455667'.
_VISIT_NUMBER = re.compile(
    r"(?i)\b(?:visit|encounter|enc|fin|csn|account|acct)\s*(?:#|no\.?|number|num|id)?\s*[:#]?\s*"
    r"([a-z0-9][a-z0-9-]{4,})\b"
)
# 'continued', "cont'd", '(over)', 'see next page', 'continued on next page'.
_CONTINUED = re.compile(r"(?i)\b(?:continued|cont'?d\.?|see\s+next\s+page|\(\s*over\s*\)|over\s*>>)\s*\W*$")
_DATE = re.compile(
    r"(?i)\b(?:\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}"
    r"|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2},?\s+\d{2,4})\b"
)
_DIGITS = re.compile(r"\d+")
_TERMINAL = re.compile(r"[.!?:;)\]\"']\s*$")


@dataclass(frozen=True)
class Semantic:
    furniture_min_chars: int = 12
    tail_nth_word: int = 20
    char_ngram: tuple[int, int] = (3, 5)
    word_ngram: tuple[int, int] = (1, 2)
    min_df: int = 2
    char_divisor: float = 0.25
    word_divisor: float = 0.15


def normalized_line(text: str) -> str:
    """A header / footer line with its dates and numbers as placeholders, so a page number
    or a print time cannot make two otherwise identical lines differ:
    'smith, john | mrn 004812 | printed 03/28/24 | page 3 of 7' ->
    'smith, john | mrn # | printed <date> | page # of #'."""
    return _DIGITS.sub("#", _DATE.sub("<date>", (text or "").lower())).strip()


def furniture(prev: Sequence[str], cur: Sequence[str], min_chars: int) -> set[str]:
    """Normalised lines of ``min_chars`` or more printed on both pages: banner, letterhead,
    footer template."""
    a = {normalized_line(l) for l in prev if len(l) >= min_chars}
    b = {normalized_line(l) for l in cur if len(l) >= min_chars}
    return a & b


def repeated_line(prev: Sequence[str], cur: Sequence[str], min_chars: int) -> bool:
    """An identical line (normalised, exact) in the same band of both pages."""
    return bool(furniture(prev, cur, min_chars))


def _collect(lines: Sequence[str], n_words: int, *, backwards: bool) -> str:
    """Two lines, then more until ``n_words`` words are gathered."""
    picked: list[str] = []
    count = 0
    for line in (reversed(lines) if backwards else lines):
        picked.append(line)
        count += len(line.split())
        if len(picked) >= 2 and count >= n_words:
            break
    return " ".join(reversed(picked) if backwards else picked)


def tail_block(lines: Sequence[str], nth_word: int) -> str:
    """The last two body lines (by position on the page), extended back to ``nth_word``
    words."""
    return _collect(lines, nth_word, backwards=True)


def head_block(lines: Sequence[str], nth_word: int) -> str:
    """The first two body lines, extended forward to ``nth_word`` words."""
    return _collect(lines, nth_word, backwards=False)


# --- TF-IDF ----------------------------------------------------------------------------


def _char_grams(text: str, low: int, high: int) -> list[str]:
    grams: list[str] = []
    for word in re.findall(r"[a-z0-9]+", text.lower()):
        padded = f" {word} "
        for n in range(low, high + 1):
            grams.extend(padded[i : i + n] for i in range(max(1, len(padded) - n + 1)))
    return grams


def _word_grams(text: str, low: int, high: int) -> list[str]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return [" ".join(words[i : i + n]) for n in range(low, high + 1) for i in range(len(words) - n + 1)]


class TfIdf:
    """Fitted on a corpus of blocks; ``cosine(a, b)`` between two of its texts."""

    def __init__(self, documents: Sequence[str], analyzer, min_df: int) -> None:
        self.analyzer = analyzer
        counts = [Counter(analyzer(doc)) for doc in documents]
        df: Counter[str] = Counter()
        for c in counts:
            df.update(c.keys())
        n = len(documents)
        self.idf = {t: math.log((1 + n) / (1 + d)) + 1.0 for t, d in df.items() if d >= min_df}

    def vector(self, text: str) -> dict[str, float]:
        vec = {t: (1.0 + math.log(c)) * self.idf[t] for t, c in Counter(self.analyzer(text)).items() if t in self.idf}
        norm = math.sqrt(sum(v * v for v in vec.values()))
        return {t: v / norm for t, v in vec.items()} if norm else {}

    def cosine(self, a: str, b: str) -> float:
        va, vb = self.vector(a), self.vector(b)
        if len(va) > len(vb):
            va, vb = vb, va
        return sum(v * vb.get(t, 0.0) for t, v in va.items())


@dataclass
class Corpus:
    char: Optional[TfIdf]
    word: Optional[TfIdf]


def fit_corpus(blocks: Sequence[str], semantic: Semantic) -> Corpus:
    texts = [b for b in blocks if b.strip()]
    if len(texts) < 2:
        return Corpus(None, None)
    low, high = semantic.char_ngram
    wlow, whigh = semantic.word_ngram
    return Corpus(
        TfIdf(texts, lambda t: _char_grams(t, low, high), semantic.min_df),
        TfIdf(texts, lambda t: _word_grams(t, wlow, whigh), semantic.min_df),
    )


# --- the other measurements ---------------------------------------------------------------


def visit_numbers(text: str) -> set[str]:
    return {m.group(1).casefold() for m in _VISIT_NUMBER.finditer(text or "") if re.search(r"\d", m.group(1))}


def says_continued(last_lines: Sequence[str]) -> bool:
    """The page's closing lines say it carries on."""
    return any(_CONTINUED.search(line) for line in last_lines)


def cut_mid_sentence(last_line: str) -> bool:
    text = (last_line or "").strip()
    return bool(text) and not _TERMINAL.search(text)


def starts_mid_thought(first_line: str) -> bool:
    text = (first_line or "").strip()
    return bool(text) and text[0].islower()


def ramp(x: float, x0: float, y0: float, x1: float, y1: float) -> float:
    """Straight line from (x0, y0) to (x1, y1)."""
    if x1 == x0:
        return y1
    return y0 + (y1 - y0) * (x - x0) / (x1 - x0)


def fill_value(end: float, cfg: dict[str, float]) -> float:
    """Where the text ends (fraction of the page height) -> -1 (full, continues) .. +1 (the
    ending band) .. 0 (nearly empty). docs §5.1."""
    full, taper, high, low, empty = (cfg["full_at"], cfg["taper_at"], cfg["band_high"], cfg["band_low"], cfg["empty_at"])
    if end >= full:
        return -1.0
    if end >= taper:
        return ramp(end, taper, 0.0, full, -1.0)
    if end >= high:
        return ramp(end, high, 1.0, taper, 0.0)
    if end >= low:
        return 1.0
    if end >= empty:
        return ramp(end, empty, 0.0, low, 1.0)
    return 0.0


def start_value(gap: float, cfg: dict[str, float]) -> float:
    """Space between the header furniture and the first body line -> -0.5 (straight on) ..
    +1 (air above a title block). docs §5.4."""
    straight, near, air = cfg["straight_at"], cfg["near_at"], cfg["air_at"]
    if gap <= straight:
        return -0.5
    if gap <= near:
        return ramp(gap, straight, -0.5, near, 0.0)
    if gap <= air:
        return ramp(gap, near, 0.0, air, 1.0)
    return 1.0


def section_ranks(headers: Sequence[Any], rank_of: dict[str, int]) -> list[int]:
    """Note-order ranks of the page's section headings (dicts with matched_canonical / text,
    or plain heading texts)."""
    ranks = []
    for header in headers or []:
        if isinstance(header, dict):
            name = str(header.get("matched_canonical") or header.get("text") or "")
        else:
            name = str(header)
        name = name.strip().rstrip(":").strip().casefold()
        if name in rank_of:
            ranks.append(rank_of[name])
    return ranks


def section_forward(prev: list[int], cur: list[int], steps: int) -> float:
    """How far the next page moves forward in note order, 0..1 (backwards counts 0)."""
    if not prev or not cur or not steps:
        return 0.0
    direction = (min(cur) - max(prev)) / steps * 4
    return max(0.0, min(1.0, direction))


def clip(value: float) -> float:
    return max(0.0, min(1.0, value))
