"""The weighted continue / new-document signals for one pair of pages.

docs/CONTINUITY_LOGIC.md §4–§7. Every value is in [0, 1]; the engine multiplies each by
its weight (``continuity_canon.json`` ``weights``) and sums. A signal that cannot speak
(no section headers, no visit number) is 0 and so adds nothing either way.

Text similarity is a small TF-IDF written here (no new dependency): character 3–5 grams
inside words and word 1–2 grams, ``min_df`` 2, sublinear tf, fitted on every tail / head
block of the chart (never per pair: idf needs the corpus to down-weight ubiquitous
clinical vocabulary).
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

# The signals the engine knows; the canon's weights may name only these.
SIGNALS = (
    "section_forward",
    "page_type_change",
    "cur_has_title",
    "same_visit_number",
    "page_fill",
    "header_line_repeats",
    "prev_signed",
    "body_cosine_char",
    "full_path_same",
    "footer_line_repeats",
    "body_cosine_word",
    "cur_opens_note",
    "prev_signed_in_tail",
)

# 'Visit #: 12345', 'Encounter No 9988-1', 'FIN 100234', 'CSN: 4455667'.
_VISIT_NUMBER = re.compile(
    r"(?i)\b(?:visit|encounter|enc|fin|csn|account|acct)\s*(?:#|no\.?|number|num|id)?\s*[:#]?\s*"
    r"([a-z0-9][a-z0-9-]{4,})\b"
)


@dataclass(frozen=True)
class Semantic:
    furniture_min_chars: int = 12
    tail_nth_word: int = 20
    char_ngram: tuple[int, int] = (3, 5)
    word_ngram: tuple[int, int] = (1, 2)
    min_df: int = 2
    char_divisor: float = 0.25
    word_divisor: float = 0.15


@dataclass
class PairText:
    """Furniture removed: what is left of the previous page's tail and the next page's head."""

    tail: str
    head: str
    furniture: set[str] = field(default_factory=set)


def strip_furniture(prev: Sequence[str], cur: Sequence[str], min_chars: int) -> tuple[list[str], list[str], set[str]]:
    """Lines printed on both pages (banner, footer template) are furniture, not body."""
    repeated = {l for l in prev if len(l) >= min_chars} & {l for l in cur if len(l) >= min_chars}
    return [l for l in prev if l not in repeated], [l for l in cur if l not in repeated], repeated


def _paragraphs(lines: Sequence[str]) -> list[str]:
    """Blank-line blocks are rare in OCR: consecutive lines in threes."""
    return [" ".join(lines[i : i + 3]) for i in range(0, len(lines), 3)]


def tail_block(lines: Sequence[str], nth_word: int) -> str:
    """The last paragraph, walking back until ``nth_word`` words are gathered."""
    paras = _paragraphs(lines)
    out: list[str] = []
    count = 0
    for para in reversed(paras):
        out.insert(0, para)
        count += len(para.split())
        if count >= nth_word:
            break
    return " ".join(out)


def head_block(lines: Sequence[str], nth_word: int) -> str:
    """The first paragraph, walking forward until ``nth_word`` words are gathered."""
    out: list[str] = []
    count = 0
    for para in _paragraphs(lines):
        out.append(para)
        count += len(para.split())
        if count >= nth_word:
            break
    return " ".join(out)


def pair_text(prev_lines: Sequence[str], cur_lines: Sequence[str], semantic: Semantic) -> PairText:
    prev_body, cur_body, furniture = strip_furniture(prev_lines, cur_lines, semantic.furniture_min_chars)
    return PairText(tail_block(prev_body, semantic.tail_nth_word), head_block(cur_body, semantic.tail_nth_word), furniture)


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


# --- the other signals ------------------------------------------------------------------


def visit_numbers(text: str) -> set[str]:
    return {m.group(1).casefold() for m in _VISIT_NUMBER.finditer(text or "") if re.search(r"\d", m.group(1))}


def graded_fill(fill: Optional[float], ended_at: float, continues_at: float) -> float:
    """0 at or below ``ended_at`` (the document ended there), 1 at or above ``continues_at``."""
    if fill is None:
        return 0.0
    if fill <= ended_at:
        return 0.0
    if fill >= continues_at:
        return 1.0
    return (fill - ended_at) / (continues_at - ended_at)


def section_ranks(headers: Sequence[dict[str, Any]], rank_of: dict[str, int]) -> list[int]:
    ranks = []
    for header in headers or []:
        name = str(header.get("matched_canonical") or header.get("text") or "").strip().casefold()
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
