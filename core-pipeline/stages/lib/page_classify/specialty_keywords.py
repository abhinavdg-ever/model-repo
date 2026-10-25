"""A page's specialty from its words (``keyword-canon/specialty_canon.json``).

The axes gate reads it when the model asserts no specialty (Unknown, unsure or
undertrained). The strongest evidence is the practice name in the letterhead, so a
``header`` term in the page's top band (``matching.header_band_fraction`` of its lines)
scores ``header_weight``; a header term lower down, or a ``body`` term anywhere, scores
``body_weight``. Each term counts once. The best specialty needs ``min_score`` and a
lead of ``margin_to_decide`` over the next; otherwise the page has none here (a tie
between two specialties is not a coin toss).

The lexicon is a copy of ``training/taxonomy/rules/specialty.json``;
``tests/test_page_axes.py`` fails when the two differ.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional

from stages.lib.canon_store import CANON_DIR, CanonFile


@dataclass(frozen=True)
class _Lexicon:
    header_fraction: float
    header_weight: float
    body_weight: float
    min_score: float
    margin: float
    # specialty -> (header patterns, body patterns)
    terms: dict[str, tuple[tuple[re.Pattern[str], ...], tuple[re.Pattern[str], ...]]]


def _word(term: str) -> re.Pattern[str]:
    return re.compile(r"(?<![a-z0-9])" + re.escape(term.casefold()) + r"(?![a-z0-9])")


def _build(data: dict[str, Any]) -> _Lexicon:
    m = data["matching"]
    terms = {
        name: (tuple(_word(t) for t in entry.get("header", ())), tuple(_word(t) for t in entry.get("body", ())))
        for name, entry in data["specialties"].items()
        if isinstance(entry, dict)
    }
    return _Lexicon(float(m["header_band_fraction"]), float(m["header_weight"]), float(m["body_weight"]),
                    float(m["min_score"]), float(m["margin_to_decide"]), terms)


_LEXICON: CanonFile[_Lexicon] = CanonFile(CANON_DIR / "specialty_canon.json", _build)


def scores(text: str) -> dict[str, float]:
    """Every specialty with a term on the page, and its score."""
    lex = _LEXICON.get()
    lines = [line.casefold() for line in (text or "").splitlines() if line.strip()]
    if not lines:
        return {}
    band = max(1, round(lex.header_fraction * len(lines)))
    top, whole = "\n".join(lines[:band]), "\n".join(lines)
    out: dict[str, float] = {}
    for name, (header, body) in lex.terms.items():
        score = 0.0
        for pattern in header:
            if pattern.search(top):
                score += lex.header_weight
            elif pattern.search(whole):
                score += lex.body_weight
        score += sum(lex.body_weight for pattern in body if pattern.search(whole))
        if score:
            out[name] = score
    return out


def specialty(text: str) -> tuple[Optional[str], Optional[float]]:
    """(specialty, score) when one clears ``min_score`` with a lead of
    ``margin_to_decide``; (None, None) otherwise."""
    lex = _LEXICON.get()
    ranked = sorted(scores(text).items(), key=lambda kv: -kv[1])
    if not ranked or ranked[0][1] < lex.min_score:
        return None, None
    second = ranked[1][1] if len(ranked) > 1 else 0.0
    if ranked[0][1] - second < lex.margin:
        return None, None
    return ranked[0]


def confidence(score: float) -> float:
    """A keyword score as a confidence: 0.5 at ``min_score``, 1.0 at twice that."""
    return round(min(1.0, score / (2 * _LEXICON.get().min_score)), 4)
